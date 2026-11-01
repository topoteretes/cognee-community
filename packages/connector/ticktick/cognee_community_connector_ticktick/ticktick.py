"""DLT source for TickTick tasks and projects (full-snapshot sync + forget-on-delete).

Pull TickTick projects, open tasks, and completed tasks into cognee, then hand the
resulting ``dlt`` source to :func:`cognee.remember`::

    import cognee
    from cognee_community_connector_ticktick import ticktick_source

    await cognee.remember(
        ticktick_source(access_token="…"),
        dataset_name="ticktick",
        primary_key="id",
        write_disposition="replace",
        max_rows_per_table=0,
    )

Design
------
* **Auth** — OAuth 2.0 ``authorization_code`` with ``tasks:read``. Pass an
  ``access_token`` directly, or use :func:`get_ticktick_token` once to obtain and
  cache a token via the browser installed-app flow.
* **Primary key** — prefixed ids (``task:…``, ``project:…``). Combined with
  ``write_disposition="replace"`` this gives a full authoritative snapshot.
* **No server-side incremental cursor** — TickTick exposes ``modifiedTime`` on
  tasks but no listing endpoint accepts it as a filter. Each run therefore builds
  a complete in-scope snapshot. Unchanged rows keep a stable content-hash
  ``data_id``, so they are not re-cognified.
* **Forget-on-delete** — TickTick has no delete feed. Items that vanish from the
  snapshot drop out of staging on ``replace`` and cognee's existing
  ``orphan_cleanup`` removes them from the graph + vector stores.
* **Completed tasks** — ``GET /project/{id}/data`` returns open tasks only.
  Completed tasks are fetched via ``POST /task/completed`` with time windows.
  That endpoint caps responses at 200; full windows are bisected recursively.
  An unsplittable full window aborts the run rather than publishing a partial
  snapshot that could mass-forget live tasks.
* **Inbox** — Inbox is not returned by ``GET /project``. Use the
  ``GET /project/inbox/data`` shortcut (or pass ``"inbox"`` in
  ``selected_project_ids``). The real per-user id is ``inbox<userId>`` and is
  resolved from task ``projectId`` values when needed for completed-task queries.
* **Document path** — tasks and projects are prose, so the source sets
  ``DOCUMENT_SOURCE_ATTR`` and rows flow through cognify.

Privacy
-------
This connector reads the content of your TickTick account. It is **opt-in**:
nothing is fetched until you construct a source and call ``remember``. Scope
with ``selected_project_ids``, keep the token file private, and prefer a
dedicated dataset so you can wipe it in one call.
"""

from __future__ import annotations

import base64
import json
import os
import re
import time
import webbrowser
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any
from urllib.parse import parse_qs, urlencode, urlparse

from cognee.shared.logging_utils import get_logger
from cognee.tasks.ingestion.dlt_utils import DOCUMENT_SOURCE_ATTR

logger = get_logger("ticktick_connector")

API_BASE = "https://api.ticktick.com/open/v1"
OAUTH_AUTHORIZE_URL = "https://ticktick.com/oauth/authorize"
OAUTH_TOKEN_URL = "https://ticktick.com/oauth/token"
OAUTH_SCOPE = "tasks:read"

TICKTICK_TABLE_NAME = "ticktick_items"
TICKTICK_SOURCE_NAME = "ticktick"

_PRIORITY = {0: "", 1: "Low", 3: "Medium", 5: "High"}
_STATUS = {0: "Open", -1: "Abandoned", 2: "Completed"}
_MAX_RETRIES = 3
_COMPLETED_PAGE_LIMIT = 200
_INBOX_ID_RE = re.compile(r"^inbox\d+$")
_TIMEOUT = (10, 30)

_EXTRA_HINT = (
    "The TickTick connector requires dlt and requests. "
    'Install with: pip install "cognee-community-connector-ticktick"'
)


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------
class TickTickError(RuntimeError):
    """Base class for TickTick connector errors."""


class TickTickAuthError(TickTickError):
    """TickTick rejected the access token (HTTP 401)."""


class TickTickAPIError(TickTickError):
    """TickTick answered with an error, or could not be reached."""


class TickTickSnapshotError(TickTickError):
    """A complete authoritative snapshot could not be built safely."""


# ---------------------------------------------------------------------------
# OAuth helper (one-time token acquisition)
# ---------------------------------------------------------------------------
def get_ticktick_token(
    client_id: str | None = None,
    client_secret: str | None = None,
    *,
    redirect_uri: str = "http://localhost:8080/callback",
    token_path: str = ".ticktick-token",
    force: bool = False,
) -> str:
    """Obtain a TickTick OAuth access token via the browser installed-app flow.

    On first run this opens a browser for consent, exchanges the authorization
    code for an access token, and caches it at ``token_path`` (mode ``0o600``).
    Later runs reuse the cached token unless ``force=True``.

    TickTick documents only ``grant_type=authorization_code`` (no refresh token),
    so when the token expires delete ``token_path`` (or pass ``force=True``) and
    reconnect.

    Args:
        client_id: OAuth client id (or ``TICKTICK_CLIENT_ID``).
        client_secret: OAuth client secret (or ``TICKTICK_CLIENT_SECRET``).
        redirect_uri: Must match the redirect URI registered in the developer
            console. Defaults to a local callback on port 8080.
        token_path: Where the cached access token is read/written.
        force: Ignore any cached token and run the browser flow again.

    Returns:
        The access token string.
    """
    resolved_id = client_id or os.environ.get("TICKTICK_CLIENT_ID")
    resolved_secret = client_secret or os.environ.get("TICKTICK_CLIENT_SECRET")
    if not resolved_id or not resolved_secret:
        raise ValueError(
            "TickTick OAuth requires client_id and client_secret "
            "(pass them or set TICKTICK_CLIENT_ID / TICKTICK_CLIENT_SECRET)."
        )

    if not force and os.path.exists(token_path):
        cached = _read_token_file(token_path)
        if cached:
            return cached

    code = _run_oauth_callback(resolved_id, redirect_uri)
    token = _exchange_code(resolved_id, resolved_secret, code, redirect_uri)
    _write_token_file(token_path, token)
    return token


def _read_token_file(token_path: str) -> str | None:
    try:
        with open(token_path, encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, json.JSONDecodeError, ValueError):
        return None
    token = data.get("access_token") if isinstance(data, dict) else None
    return token if isinstance(token, str) and token else None


def _write_token_file(token_path: str, access_token: str) -> None:
    payload = json.dumps({"access_token": access_token})
    fd = os.open(token_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(payload)
    os.chmod(token_path, 0o600)


def _run_oauth_callback(client_id: str, redirect_uri: str) -> str:
    """Open the browser and capture the authorization code from the redirect."""
    parsed = urlparse(redirect_uri)
    host = parsed.hostname or "localhost"
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    path = parsed.path or "/callback"

    result: dict[str, str] = {}

    class _Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            query = parse_qs(urlparse(self.path).query)
            if self.path.startswith(path) and "code" in query:
                result["code"] = query["code"][0]
                self.send_response(200)
                self.send_header("Content-Type", "text/html")
                self.end_headers()
                self.wfile.write(
                    b"<html><body><h2>TickTick authorization complete.</h2>"
                    b"<p>You can close this window.</p></body></html>"
                )
            else:
                self.send_response(400)
                self.end_headers()
                self.wfile.write(b"Missing authorization code.")

        def log_message(self, format, *args):
            return

    params = urlencode(
        {
            "client_id": client_id,
            "scope": OAUTH_SCOPE,
            "state": "cognee",
            "redirect_uri": redirect_uri,
            "response_type": "code",
        }
    )
    auth_url = f"{OAUTH_AUTHORIZE_URL}?{params}"
    server = HTTPServer((host, port), _Handler)
    logger.info("Opening TickTick authorization URL in your browser…")
    webbrowser.open(auth_url)
    # Handle a single callback request, then shut down.
    server.handle_request()
    server.server_close()
    if "code" not in result:
        raise TickTickAuthError("OAuth callback did not return an authorization code.")
    return result["code"]


def _exchange_code(client_id: str, client_secret: str, code: str, redirect_uri: str) -> str:
    """Exchange an authorization code for an access token."""
    try:
        import requests
    except ImportError as exc:  # pragma: no cover - optional dependency
        raise ImportError(_EXTRA_HINT) from exc

    basic = base64.b64encode(f"{client_id}:{client_secret}".encode()).decode()
    response = requests.post(
        OAUTH_TOKEN_URL,
        headers={
            "Authorization": f"Basic {basic}",
            "Content-Type": "application/x-www-form-urlencoded",
        },
        data={
            "code": code,
            "grant_type": "authorization_code",
            "scope": OAUTH_SCOPE,
            "redirect_uri": redirect_uri,
        },
        timeout=_TIMEOUT,
    )
    if response.status_code == 401:
        raise TickTickAuthError("TickTick rejected the OAuth client credentials (HTTP 401).")
    if response.status_code >= 400:
        raise TickTickAPIError(f"TickTick token exchange failed: HTTP {response.status_code}")
    try:
        body = response.json()
    except ValueError as exc:
        raise TickTickAPIError("TickTick token exchange returned non-JSON.") from exc
    token = body.get("access_token")
    if not isinstance(token, str) or not token:
        raise TickTickAPIError("TickTick token exchange response had no access_token.")
    return token


# ---------------------------------------------------------------------------
# HTTP session / request helpers
# ---------------------------------------------------------------------------
def _build_session(access_token: str) -> Any:
    """Return a ``requests.Session`` authenticated with a Bearer token."""
    try:
        import requests
    except ImportError as exc:  # pragma: no cover - optional dependency
        raise ImportError(_EXTRA_HINT) from exc

    token = (access_token or "").strip()
    if not token or not token.isascii() or not token.isprintable() or " " in token:
        raise ValueError("The TickTick access token is empty or has an invalid format.")

    session = requests.Session()
    session.headers.update(
        {
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
            "Content-Type": "application/json",
        }
    )
    return session


def _request(
    session: Any,
    method: str,
    url: str,
    *,
    params: dict | None = None,
    json_body: dict | None = None,
) -> Any:
    """Call the TickTick API, retrying rate-limit / transient errors."""
    last_exc: Exception | None = None
    for attempt in range(_MAX_RETRIES):
        try:
            response = session.request(
                method,
                url,
                params=params,
                json=json_body,
                timeout=_TIMEOUT,
            )
        except Exception as exc:  # network / timeout
            last_exc = exc
            if attempt == _MAX_RETRIES - 1:
                raise TickTickAPIError(f"TickTick request failed: {type(exc).__name__}") from None
            time.sleep(2**attempt)
            continue

        status = response.status_code
        if status == 401:
            raise TickTickAuthError("TickTick rejected the access token (HTTP 401).")
        if status in (429, 500, 502, 503, 504):
            if attempt == _MAX_RETRIES - 1:
                raise TickTickAPIError(f"TickTick request failed: HTTP {status}")
            retry_after = response.headers.get("Retry-After")
            try:
                delay = float(retry_after) if retry_after else float(2**attempt)
            except (TypeError, ValueError):
                delay = float(2**attempt)
            time.sleep(delay)
            continue
        if status >= 400:
            raise TickTickAPIError(f"TickTick request failed: HTTP {status}")

        if not response.content:
            return {}
        try:
            return response.json()
        except ValueError as exc:
            raise TickTickAPIError("TickTick response was not JSON.") from exc

    raise TickTickAPIError(f"TickTick request failed: {last_exc}")  # pragma: no cover


def _api_get(session: Any, path: str, params: dict | None = None) -> Any:
    return _request(session, "GET", f"{API_BASE}{path}", params=params)


def _api_post(session: Any, path: str, json_body: dict | None = None) -> Any:
    return _request(session, "POST", f"{API_BASE}{path}", json_body=json_body)


# ---------------------------------------------------------------------------
# Time helpers (completed-task windows)
# ---------------------------------------------------------------------------
def _now_iso() -> str:
    return datetime.now(tz=UTC).strftime("%Y-%m-%dT%H:%M:%S+0000")


def _days_ago_iso(days: int) -> str:
    moment = datetime.now(tz=UTC) - timedelta(days=max(0, days))
    return moment.strftime("%Y-%m-%dT%H:%M:%S+0000")


def _parse_ticktick_time(value: str) -> datetime:
    """Parse TickTick's ``yyyy-MM-dd'T'HH:mm:ssZ`` timestamps."""
    # Accept both +0000 and Z suffixes.
    cleaned = value.strip()
    if cleaned.endswith("Z"):
        cleaned = cleaned[:-1] + "+0000"
    # Normalize +0000 → +00:00 for fromisoformat on 3.11+.
    if len(cleaned) >= 5 and cleaned[-5] in ("+", "-") and ":" not in cleaned[-5:]:
        cleaned = cleaned[:-2] + ":" + cleaned[-2:]
    return datetime.fromisoformat(cleaned)


def _format_ticktick_time(moment: datetime) -> str:
    utc = moment.astimezone(UTC)
    return utc.strftime("%Y-%m-%dT%H:%M:%S+0000")


def _midpoint(start: str, end: str) -> str:
    start_dt = _parse_ticktick_time(start)
    end_dt = _parse_ticktick_time(end)
    mid = start_dt + (end_dt - start_dt) / 2
    # Truncate to whole seconds so windows can shrink to a single second.
    mid = mid.replace(microsecond=0)
    return _format_ticktick_time(mid)


# ---------------------------------------------------------------------------
# API data fetchers
# ---------------------------------------------------------------------------
def _list_projects(session: Any) -> list[dict]:
    """``GET /project`` — user projects (excludes Inbox)."""
    data = _api_get(session, "/project")
    if not isinstance(data, list):
        raise TickTickAPIError("TickTick /project response was not a list.")
    return [p for p in data if isinstance(p, dict)]


def _get_project_data(session: Any, project_id: str) -> dict:
    """``GET /project/{id}/data`` (or ``/project/inbox/data`` for Inbox)."""
    path = f"/project/{project_id}/data"
    data = _api_get(session, path)
    if not isinstance(data, dict):
        raise TickTickAPIError(f"TickTick {path} response was not an object.")
    return data


def _get_completed_tasks(
    session: Any,
    project_ids: list[str],
    start_date: str,
    end_date: str,
) -> list[dict]:
    """``POST /task/completed`` for a completedTime window."""
    body = {
        "projectIds": list(project_ids),
        "startDate": start_date,
        "endDate": end_date,
    }
    data = _api_post(session, "/task/completed", json_body=body)
    # Some clients return a bare list; others wrap it.
    if isinstance(data, list):
        return [t for t in data if isinstance(t, dict)]
    if isinstance(data, dict):
        tasks = data.get("tasks") or data.get("data") or []
        if isinstance(tasks, list):
            return [t for t in tasks if isinstance(t, dict)]
    raise TickTickAPIError("TickTick /task/completed response was not a task list.")


def _fetch_completed_safe(
    session: Any,
    project_ids: list[str],
    start_date: str,
    end_date: str,
) -> list[dict]:
    """Recursively fetch completed tasks, bisecting windows that hit the 200 cap.

    A full window that cannot be narrowed further raises
    :class:`TickTickSnapshotError` so a partial snapshot is never published.
    """
    if not project_ids:
        return []

    tasks = _get_completed_tasks(session, project_ids, start_date, end_date)
    if len(tasks) < _COMPLETED_PAGE_LIMIT:
        return tasks

    mid = _midpoint(start_date, end_date)
    if mid in (start_date, end_date):
        raise TickTickSnapshotError(
            f"Completed-task window [{start_date}, {end_date}] cannot be narrowed "
            f"but returned {len(tasks)} tasks (cap={_COMPLETED_PAGE_LIMIT}). "
            "Aborting to avoid a partial snapshot."
        )

    left = _fetch_completed_safe(session, project_ids, start_date, mid)
    right = _fetch_completed_safe(session, project_ids, mid, end_date)
    seen = {t.get("id") for t in left}
    return left + [t for t in right if t.get("id") not in seen]


def _resolve_inbox_id(tasks: list[dict]) -> str | None:
    """Extract the real ``inbox<userId>`` project id from task ``projectId`` values."""
    for task in tasks:
        project_id = task.get("projectId")
        if isinstance(project_id, str) and _INBOX_ID_RE.match(project_id):
            return project_id
    return None


# ---------------------------------------------------------------------------
# Deterministic rendering
# ---------------------------------------------------------------------------
def _lines(fields: list[tuple[str, str]]) -> list[str]:
    return [f"{label}: {value}" for label, value in fields if value]


def _render_task(task: dict, project_name: str) -> dict[str, Any]:
    """Flatten a TickTick task into a document row.

    Volatile fields (``modifiedTime``, ``sortOrder``, ``etag``, dates) are
    excluded so a metadata-only bump does not churn the content-hash ``data_id``.
    """
    tags = task.get("tags") or []
    if not isinstance(tags, list):
        tags = []
    tag_text = ", ".join(str(t) for t in tags if t)

    fields = [
        ("Status", _STATUS.get(task.get("status", 0), "")),
        ("Priority", _PRIORITY.get(task.get("priority", 0), "")),
        ("Project", (project_name or "").strip()),
        ("Tags", tag_text),
        ("Kind", str(task.get("kind") or "").strip()),
    ]
    parts = _lines(fields)

    title = (task.get("title") or "").strip() or "Untitled task"
    content = (task.get("content") or "").strip()
    if content:
        parts.extend(["", content])
    desc = (task.get("desc") or "").strip()
    if desc:
        parts.extend(["", desc])

    items = task.get("items") or []
    if isinstance(items, list) and items:
        parts.append("")
        ordered = sorted(
            (i for i in items if isinstance(i, dict)),
            key=lambda i: (i.get("sortOrder") is None, i.get("sortOrder") or 0, str(i.get("id"))),
        )
        for item in ordered:
            check = "x" if item.get("status") == 1 else " "
            item_title = (item.get("title") or "").strip()
            if item_title:
                parts.append(f"- [{check}] {item_title}")

    return {
        "id": f"task:{task['id']}",
        "title": title,
        "content": "\n".join(parts).strip() or title,
        "url": "",
    }


def _render_project(project: dict) -> dict[str, Any]:
    """Flatten a TickTick project into a document row."""
    title = (project.get("name") or "").strip() or "Untitled project"
    fields = [
        ("Kind", str(project.get("kind") or "").strip()),
        ("View", str(project.get("viewMode") or "").strip()),
    ]
    parts = _lines(fields)
    return {
        "id": f"project:{project['id']}",
        "title": title,
        "content": "\n".join(parts).strip() or title,
        "url": "",
    }


# ---------------------------------------------------------------------------
# Snapshot builder
# ---------------------------------------------------------------------------
def build_snapshot(
    session: Any,
    selected_project_ids: list[str] | None = None,
    *,
    include_completed: bool = True,
    completed_since_days: int = 90,
) -> list[dict[str, Any]]:
    """Build a complete snapshot of all selected TickTick content.

    The snapshot is **authoritative**: items absent from it are deletions under
    ``write_disposition="replace"``. A mid-run API error must abort (never yield
    a partial snapshot).
    """
    rows: list[dict[str, Any]] = []
    projects = _list_projects(session)
    project_map = {str(p["id"]): p for p in projects if p.get("id") is not None}

    include_inbox = False
    target_ids: list[str] = []
    if selected_project_ids is None:
        target_ids = list(project_map.keys())
        include_inbox = True
    else:
        for pid in selected_project_ids:
            if pid == "inbox":
                include_inbox = True
            elif pid in project_map:
                target_ids.append(pid)

    for pid in target_ids:
        rows.append(_render_project(project_map[pid]))

    open_tasks: list[dict] = []
    for pid in target_ids:
        data = _get_project_data(session, pid)
        proj_name = str(project_map[pid].get("name") or "")
        for task in data.get("tasks") or []:
            if not isinstance(task, dict) or not task.get("id"):
                continue
            rows.append(_render_task(task, proj_name))
            open_tasks.append(task)

    inbox_tasks: list[dict] = []
    if include_inbox:
        inbox_data = _get_project_data(session, "inbox")
        for task in inbox_data.get("tasks") or []:
            if not isinstance(task, dict) or not task.get("id"):
                continue
            rows.append(_render_task(task, "Inbox"))
            inbox_tasks.append(task)
            open_tasks.append(task)

    if include_completed and (target_ids or include_inbox):
        fetch_ids = list(target_ids)
        inbox_real_id = _resolve_inbox_id(inbox_tasks) or _resolve_inbox_id(open_tasks)
        if include_inbox and inbox_real_id:
            fetch_ids.append(inbox_real_id)
        if fetch_ids:
            end = _now_iso()
            start = _days_ago_iso(completed_since_days)
            completed = _fetch_completed_safe(session, fetch_ids, start, end)
            seen = {r["id"] for r in rows}
            for task in completed:
                if not task.get("id"):
                    continue
                row_id = f"task:{task['id']}"
                if row_id in seen:
                    continue
                proj_id = str(task.get("projectId") or "")
                if proj_id in project_map:
                    proj_name = str(project_map[proj_id].get("name") or "")
                elif _INBOX_ID_RE.match(proj_id) or proj_id == "inbox":
                    proj_name = "Inbox"
                else:
                    proj_name = ""
                rows.append(_render_task(task, proj_name))
                seen.add(row_id)

    return rows


# ---------------------------------------------------------------------------
# Public factory
# ---------------------------------------------------------------------------
def ticktick_source(
    *,
    access_token: str | None = None,
    client_id: str | None = None,
    client_secret: str | None = None,
    redirect_uri: str = "http://localhost:8080/callback",
    token_path: str = ".ticktick-token",
    selected_project_ids: list[str] | None = None,
    include_completed: bool = True,
    completed_since_days: int = 90,
    session: Any = None,
):
    """Return a ``dlt`` source that yields TickTick items for ``remember``.

    Args:
        access_token: Bearer token. Falls back to ``TICKTICK_ACCESS_TOKEN``,
            then to a cached token at ``token_path``, then (if ``client_id`` /
            ``client_secret`` are set) to :func:`get_ticktick_token`.
        client_id / client_secret: OAuth credentials used only when no token is
            available and the browser flow should run.
        redirect_uri / token_path: OAuth callback URI and token cache path.
        selected_project_ids: Restrict to these project ids. Pass ``"inbox"`` to
            include Inbox. ``None`` syncs every project the token can see plus
            Inbox.
        include_completed: Also fetch completed tasks via ``POST /task/completed``.
        completed_since_days: How far back to fetch completed tasks.
        session: Pre-built HTTP session (mainly a test-injection point).

    Returns:
        A ``dlt`` source configured with ``primary_key="id"`` and
        ``write_disposition="replace"``, opted into the document ingestion path.
    """
    try:
        import dlt
    except ImportError as exc:
        raise ImportError(_EXTRA_HINT) from exc

    if session is None:
        token = (
            access_token or os.environ.get("TICKTICK_ACCESS_TOKEN") or _read_token_file(token_path)
        )
        if not token:
            if client_id or client_secret or os.environ.get("TICKTICK_CLIENT_ID"):
                token = get_ticktick_token(
                    client_id,
                    client_secret,
                    redirect_uri=redirect_uri,
                    token_path=token_path,
                )
            else:
                raise ValueError(
                    "ticktick_source requires access_token (or TICKTICK_ACCESS_TOKEN), "
                    "an injected session, or OAuth client_id/client_secret."
                )
        client = _build_session(token)
    else:
        client = session

    @dlt.resource(
        name=TICKTICK_TABLE_NAME,
        primary_key="id",
        write_disposition="replace",
    )
    def ticktick_items() -> Iterator[dict[str, Any]]:
        # Full-snapshot sync: each run replaces staging with exactly the items
        # currently visible for the selected scope. Deleted tasks drop out of
        # TickTick's listings, so they fall out of staging and cognee's
        # orphan_cleanup forgets them. A mid-run error must abort — a partial
        # snapshot under replace would silently forget live memory.
        count = 0
        for row in build_snapshot(
            client,
            selected_project_ids,
            include_completed=include_completed,
            completed_since_days=completed_since_days,
        ):
            count += 1
            yield row
        logger.info("TickTick: synced %d item(s).", count)

    @dlt.source(name=TICKTICK_SOURCE_NAME)
    def _ticktick():
        return ticktick_items

    source = _ticktick()
    # Opt into the document ingestion path (row → text document → cognify).
    # resolve_dlt_sources reads this marker; it never imports this connector.
    setattr(source, DOCUMENT_SOURCE_ATTR, TICKTICK_SOURCE_NAME)
    return source
