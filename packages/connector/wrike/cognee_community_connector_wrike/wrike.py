"""Wrike connector for cognee: a ``dlt`` source that syncs Wrike folders and tasks into memory.

Auth
----
Two ways in, both ending in the same ``Authorization: bearer <token>`` header:

* **Permanent token**: ``access_token`` (or ``WRIKE_ACCESS_TOKEN``), used as is.
* **OAuth 2.0**: ``client_id`` + ``client_secret`` + ``token_path``. Each run
  exchanges the refresh token in ``token_path`` for a fresh access token.
  Wrike rotates the refresh token on every refresh and the old one stops
  working at once (tested), so the new token response is written back to
  ``token_path`` before any API call. The write goes to a temp file that is
  renamed over the old one, so a crash can never leave a half-written file.
  The browser consent that produces the first refresh token is a one-time
  step, documented in the README; this module never opens a browser.

Wrike keeps each account in one data centre (``www.wrike.com``,
``app-eu.wrike.com``, ...). The OAuth token response names it in ``host``; a
permanent-token user sets ``host`` / ``WRIKE_HOST``.

Scope
-----
``folder_ids`` picks what to ingest: those folders, everything below them, and
their tasks. Without it, everything the token can see is ingested. Wrike works
out task scope itself (``/folders/{id}/tasks?descendants=true&subTasks=true``):
a task can sit in several folders, and a subtask's parent is the account root
rather than a folder, so matching parents by hand would miss both.

What is ingested
----------------
One row per task and per folder, ``{id, title, content, url, _deleted}`` with
ids ``task:<id>`` / ``folder:<id>``. The resource sets cognee's document-source
marker, so each row becomes a text document that goes through normal cognify.
Statuses, assignees and custom fields are stored by name, not by Wrike id, and
HTML descriptions and comments become plain text. A task's comments are part of
its document, so a question about a discussion finds the task it belongs to.

The text is deterministic: the same task always renders to the same bytes (no
sync time, no counts, no ``updatedDate``, comments and fields in a fixed order),
so re-syncing an unchanged task never churns the graph.
"""

import json
import os
import tempfile
import time
from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

import httpx
from cognee.shared.logging_utils import get_logger
from cognee.tasks.ingestion.dlt_utils import DOCUMENT_SOURCE_ATTR

logger = get_logger("wrike_connector")

DEFAULT_HOST = "www.wrike.com"
TOKEN_URL = "https://login.wrike.com/oauth2/token"
TABLE_NAME = "wrike_items"
SOURCE_TAG = "wrike"
# Wrike allows pageSize 1..1000 on list endpoints (1001 is rejected).
PAGE_SIZE = 1000
# /folders/{ids} accepts at most 100 ids per call.
_FOLDER_BATCH = 100
TASK_FIELDS = ["description", "customFields", "parentIds", "responsibleIds", "superTaskIds"]

_MAX_RETRIES = 5
_MAX_RETRY_DELAY = 60.0
_RETRY_STATUSES = (429, 500, 502, 503, 504)
_TIMEOUT = 30.0


class WrikeAPIError(RuntimeError):
    """A Wrike request failed. The message carries the status and Wrike's error code only."""

    def __init__(self, message: str, status: int | None = None, code: str | None = None):
        super().__init__(message)
        self.status = status
        self.code = code


class WrikeAuthError(WrikeAPIError):
    """The token was rejected, or the OAuth refresh failed."""


def _check_token(token: str, name: str) -> str:
    # Refuse a malformed secret before it is ever sent, without echoing it.
    token = (token or "").strip()
    if not token or not token.isascii() or any(c.isspace() for c in token):
        raise ValueError(f"{name} is empty or malformed.")
    return token


def _check_host(host: str) -> str:
    # The token is sent to this host, so only accept Wrike's own data centres.
    host = (host or "").strip().lower()
    if host != "wrike.com" and not host.endswith(".wrike.com"):
        raise ValueError(f"Wrike host must be a wrike.com host, got {host!r}.")
    return host


def _encode(params: dict[str, Any]) -> dict[str, str]:
    # Wrike takes lists and objects (fields, updatedDate, ...) as JSON strings.
    return {k: v if isinstance(v, str) else json.dumps(v) for k, v in params.items()}


def _retry_delay(response: httpx.Response | None, attempt: int) -> float:
    header = response.headers.get("retry-after") if response is not None else None
    try:
        delay = float(header)
    except (TypeError, ValueError):
        delay = 2.0**attempt
    return min(delay, _MAX_RETRY_DELAY)


def _error_from(response: httpx.Response) -> WrikeAPIError:
    try:
        code = response.json().get("error")
    except ValueError:
        code = None
    cls = WrikeAuthError if response.status_code == 401 else WrikeAPIError
    hint = " Check the access token, or redo the OAuth consent." if cls is WrikeAuthError else ""
    return cls(
        f"Wrike API request failed: HTTP {response.status_code} ({code or 'no error code'}).{hint}",
        status=response.status_code,
        code=code,
    )


class WrikeClient:
    """Minimal Wrike API v4 client: auth header, retries, and ``nextPageToken`` paging."""

    def __init__(
        self,
        access_token: str,
        host: str = DEFAULT_HOST,
        http: httpx.Client | None = None,
    ):
        self._base = f"https://{_check_host(host)}/api/v4"
        self._http = http or httpx.Client(timeout=_TIMEOUT)
        self._headers = {"Authorization": f"bearer {_check_token(access_token, 'access_token')}"}

    def _request(self, path: str, params: dict[str, Any]) -> dict:
        """GET one page, retrying rate limits (429), 5xx and network errors."""
        for attempt in range(_MAX_RETRIES):
            response = None
            try:
                response = self._http.get(
                    self._base + path, params=_encode(params), headers=self._headers
                )
            except httpx.TransportError as exc:
                if attempt == _MAX_RETRIES - 1:
                    raise WrikeAPIError(f"Wrike API unreachable: {type(exc).__name__}.") from exc
            else:
                if response.status_code == 200:
                    return response.json()
                if response.status_code not in _RETRY_STATUSES or attempt == _MAX_RETRIES - 1:
                    raise _error_from(response)
            delay = _retry_delay(response, attempt)
            logger.warning(
                "Wrike: retrying %s in %.1fs (%d/%d).", path, delay, attempt + 1, _MAX_RETRIES
            )
            time.sleep(delay)
        raise AssertionError("unreachable")

    def get(self, path: str, **params: Any) -> list[dict]:
        """GET ``path`` and return ``data`` from every page.

        Paging only happens when ``pageSize`` is passed (list endpoints); the
        original params are repeated on each page together with the token.
        """
        items: list[dict] = []
        while True:
            body = self._request(path, params)
            items.extend(body.get("data", []))
            token = body.get("nextPageToken")
            if not token:
                return items
            if token == params.get("nextPageToken"):
                # A token that does not advance would loop forever.
                raise WrikeAPIError(f"Wrike returned the same nextPageToken twice for {path}.")
            params = {**params, "nextPageToken": token}


def refresh_access_token(
    client_id: str,
    client_secret: str,
    token_path: str | os.PathLike,
    http: httpx.Client | None = None,
) -> tuple[str, str]:
    """Trade the refresh token in ``token_path`` for a new access token.

    Returns ``(access_token, host)``. The rotated token response is saved back
    to ``token_path`` before returning; on any failure the file is untouched.
    """
    path = Path(token_path).expanduser()
    try:
        refresh_token = json.loads(path.read_text())["refresh_token"]
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise WrikeAuthError(
            f"Could not read a refresh_token from {path} ({type(exc).__name__})."
        ) from exc

    http = http or httpx.Client(timeout=_TIMEOUT)
    try:
        response = http.post(
            TOKEN_URL,
            data={
                "client_id": _check_token(client_id, "client_id"),
                "client_secret": _check_token(client_secret, "client_secret"),
                "grant_type": "refresh_token",
                "refresh_token": _check_token(refresh_token, "refresh_token"),
            },
        )
    except httpx.TransportError as exc:
        raise WrikeAuthError(f"Wrike OAuth refresh failed: {type(exc).__name__}.") from exc

    try:
        body = response.json()
    except ValueError:
        body = {}
    if response.status_code != 200 or "access_token" not in body:
        raise WrikeAuthError(
            f"Wrike OAuth refresh failed: HTTP {response.status_code} "
            f"({body.get('error') or 'no error code'}). The refresh token may be used up; "
            "redo the OAuth consent described in the README.",
            status=response.status_code,
            code=body.get("error"),
        )

    host = _check_host(body.get("host") or DEFAULT_HOST)
    _write_private_json(path, body)
    return body["access_token"], host


def _write_private_json(path: Path, data: dict) -> None:
    """Atomically replace ``path`` with ``data``, readable by the owner only."""
    # mkstemp creates the file with mode 0600, and the rename keeps that mode.
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(data, f)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


# ---------------------------------------------------------------------------
# Rendering: Wrike objects -> deterministic text documents
# ---------------------------------------------------------------------------
_BLOCK_TAGS = {"p", "div", "br", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6", "blockquote"}


class _HTMLText(HTMLParser):
    def __init__(self):
        super().__init__()
        self.parts: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag in _BLOCK_TAGS:
            self.parts.append("\n- " if tag == "li" else "\n")

    def handle_endtag(self, tag):
        # <br /> arrives as start + end; it is one line break, not two.
        if tag in _BLOCK_TAGS and tag != "br":
            self.parts.append("\n")

    def handle_data(self, data):
        self.parts.append(data)


def html_to_text(html: str | None) -> str:
    """Wrike descriptions and comments are HTML; keep the words and the line breaks.

    An @mention is a link whose text is the person's name, so the name survives.
    """
    parser = _HTMLText()
    parser.feed(html or "")
    parser.close()
    lines = [" ".join(line.split()) for line in "".join(parser.parts).splitlines()]
    text = "\n".join(lines)
    while "\n\n\n" in text:
        text = text.replace("\n\n\n", "\n\n")
    return text.strip()


@dataclass(frozen=True)
class _Names:
    """Id -> name lookups, loaded once per run."""

    statuses: dict[str, str]
    people: dict[str, str]
    fields: dict[str, dict]

    @classmethod
    def load(cls, client: WrikeClient) -> "_Names":
        # /workflows lists only account-wide workflows; a space can define its
        # own, and its statuses appear on tasks too (tested), so load both.
        workflows = client.get("/workflows")
        for space in client.get("/spaces"):
            workflows += client.get(f"/spaces/{space['id']}/workflows")
        statuses = {
            status["id"]: status["name"]
            for workflow in workflows
            for status in workflow.get("customStatuses", [])
        }
        # Every contact type: people, groups, and robots such as Wrike's own
        # automation, which writes comments too.
        people = {
            contact["id"]: " ".join(
                part for part in (contact.get("firstName"), contact.get("lastName")) if part
            )
            for contact in client.get("/contacts")
        }
        fields = {field["id"]: field for field in client.get("/customfields")}
        return cls(statuses, people, fields)

    def person(self, contact_id: str) -> str:
        return self.people.get(contact_id) or "Unknown"


class _FolderTree:
    """Live folders (not in the Recycle Bin), their parent links and titles."""

    def __init__(self, folders: list[dict]):
        live = {f["id"]: f for f in folders if f.get("scope") in ("WsRoot", "WsFolder")}
        self.root_id = next(fid for fid, f in live.items() if f["scope"] == "WsRoot")
        self.folders = live
        # ponytail: a folder shared into two parents gets one path, the first seen.
        self.parent: dict[str, str] = {}
        for fid, folder in live.items():
            for child in folder.get("childIds", []):
                if child in live:
                    self.parent.setdefault(child, fid)

    def path(self, folder_id: str) -> str:
        titles = []
        while folder_id in self.folders and folder_id != self.root_id:
            titles.append(self.folders[folder_id]["title"])
            folder_id = self.parent.get(folder_id, self.root_id)
        return " / ".join(reversed(titles))

    def subtree(self, roots: list[str]) -> list[str]:
        seen: list[str] = []
        stack = list(roots)
        while stack:
            fid = stack.pop()
            if fid in seen or fid not in self.folders:
                continue
            seen.append(fid)
            stack.extend(self.folders[fid].get("childIds", []))
        return seen


def _lines(fields: list[tuple[str, str]]) -> list[str]:
    return [f"{label}: {value}" for label, value in fields if value]


def _custom_field_lines(values: list[dict], names: _Names) -> list[str]:
    rendered = []
    for item in values or []:
        field = names.fields.get(item.get("id"))
        value = str(item.get("value") or "").strip()
        if not field or not value:
            continue
        if field.get("type") == "Currency":
            value = f"{value} {field.get('settings', {}).get('currency', '')}".strip()
        rendered.append((field.get("title") or field["id"], value))
    return _lines(sorted(rendered))


def render_task(
    task: dict,
    comments: list[dict],
    tree: _FolderTree,
    names: _Names,
    task_titles: dict[str, str],
) -> dict:
    dates = task.get("dates") or {}
    folders = sorted(p for p in (tree.path(fid) for fid in task.get("parentIds", [])) if p)
    # A subtask's parentIds is the account root; its real home is the parent task.
    parents = sorted(t for t in (task_titles.get(i) for i in task.get("superTaskIds", [])) if t)
    fields = [
        ("Folder", "; ".join(folders)),
        ("Parent task", ", ".join(parents)),
        ("Status", names.statuses.get(task.get("customStatusId"), task.get("status", ""))),
        ("Assignees", ", ".join(sorted(names.person(i) for i in task.get("responsibleIds", [])))),
        ("Importance", task.get("importance", "")),
        ("Start", str(dates.get("start") or "")[:10]),
        ("Due", str(dates.get("due") or "")[:10]),
    ]
    parts = _lines(fields) + _custom_field_lines(task.get("customFields"), names)
    description = html_to_text(task.get("description"))
    if description:
        parts += ["", description]
    rendered = []
    for comment in sorted(comments, key=lambda c: (c.get("createdDate", ""), c.get("id", ""))):
        body = html_to_text(comment.get("text"))
        if body:
            author = names.person(comment.get("authorId", ""))
            rendered.append(f"- {author} ({comment.get('createdDate', '')[:10]}): {body}")
    if rendered:
        parts += ["", "Comments:", *rendered]
    title = (task.get("title") or "").strip() or "Untitled task"
    return {
        "id": f"task:{task['id']}",
        "title": title,
        "content": "\n".join(parts).strip() or title,
        "url": task.get("permalink") or "",
        "_deleted": False,
    }


def render_folder(folder: dict, tree: _FolderTree, names: _Names) -> dict:
    project = folder.get("project") or {}
    fields = [
        ("Path", tree.path(folder["id"])),
        ("Project status", names.statuses.get(project.get("customStatusId"), "")),
        ("Owners", ", ".join(sorted(names.person(i) for i in project.get("ownerIds", [])))),
    ]
    parts = _lines(fields) + _custom_field_lines(folder.get("customFields"), names)
    description = html_to_text(folder.get("description"))
    if description:
        parts += ["", description]
    title = (folder.get("title") or "").strip() or "Untitled folder"
    return {
        "id": f"folder:{folder['id']}",
        "title": title,
        "content": "\n".join(parts).strip() or title,
        "url": folder.get("permalink") or "",
        "_deleted": False,
    }


# ---------------------------------------------------------------------------
# Sync
# ---------------------------------------------------------------------------
def _iter_rows(client: WrikeClient, folder_ids: list[str] | None):
    """Yield one row per in-scope folder and task.

    Plain function of a client, so tests drive it with a fake client and no dlt.
    """
    tree = _FolderTree(client.get("/folders"))
    roots = list(folder_ids or [tree.root_id])
    unknown = [fid for fid in roots if fid not in tree.folders]
    if unknown:
        # A configuration problem, not a deletion: never sync around it.
        raise ValueError(f"Unknown or deleted Wrike folder id(s): {', '.join(unknown)}.")
    names = _Names.load(client)

    folder_ids_in_scope = [fid for fid in tree.subtree(roots) if fid != tree.root_id]
    for start in range(0, len(folder_ids_in_scope), _FOLDER_BATCH):
        batch = folder_ids_in_scope[start : start + _FOLDER_BATCH]
        for folder in client.get(f"/folders/{','.join(batch)}"):
            yield render_folder(folder, tree, names)

    tasks: dict[str, dict] = {}
    for root in roots:
        for task in client.get(
            f"/folders/{root}/tasks",
            descendants=True,
            subTasks=True,
            fields=TASK_FIELDS,
            pageSize=PAGE_SIZE,
        ):
            tasks.setdefault(task["id"], task)
    task_titles = {tid: task.get("title", "") for tid, task in tasks.items()}
    for task in tasks.values():
        comments = client.get(f"/tasks/{task['id']}/comments")
        yield render_task(task, comments, tree, names, task_titles)
    logger.info("Wrike: synced %d folder(s) and %d task(s).", len(folder_ids_in_scope), len(tasks))


def _env_list(name: str) -> list[str] | None:
    value = os.getenv(name, "")
    return [part.strip() for part in value.split(",") if part.strip()] or None


def wrike_source(
    folder_ids: list[str] | None = None,
    *,
    access_token: str | None = None,
    client_id: str | None = None,
    client_secret: str | None = None,
    token_path: str | os.PathLike | None = None,
    host: str | None = None,
    client: WrikeClient | None = None,
):
    """Return a ``dlt`` resource with one document row per Wrike folder and task.

    Hand it to ``cognee.remember(...)`` with ``primary_key="id"`` and
    ``write_disposition="merge"``. Every argument left as ``None`` falls back to
    its ``WRIKE_*`` environment variable (``WRIKE_FOLDER_IDS`` is comma-separated).

    Args:
        folder_ids: Folders (or projects, or spaces) to ingest, with everything
            below them. ``None`` ingests everything the token can see.
        access_token: Permanent token. Ignored when OAuth settings are given.
        client_id, client_secret, token_path: OAuth refresh. ``token_path`` is a
            JSON file holding ``refresh_token``; it is rewritten on every run.
        host: Wrike data centre for a permanent token (default ``www.wrike.com``).
        client: Pre-built client, mainly a test injection point.
    """
    try:
        import dlt
    except ImportError as exc:
        raise ImportError('The Wrike connector requires dlt: pip install "cognee[dlt]".') from exc

    folder_ids = folder_ids or _env_list("WRIKE_FOLDER_IDS")
    access_token = access_token or os.getenv("WRIKE_ACCESS_TOKEN")
    client_id = client_id or os.getenv("WRIKE_CLIENT_ID")
    client_secret = client_secret or os.getenv("WRIKE_CLIENT_SECRET")
    token_path = token_path or os.getenv("WRIKE_TOKEN_PATH")
    host = host or os.getenv("WRIKE_HOST") or DEFAULT_HOST
    use_oauth = bool(client_id or client_secret or token_path)
    if client is None and use_oauth and not (client_id and client_secret and token_path):
        raise ValueError("OAuth needs all of client_id, client_secret and token_path.")
    if client is None and not use_oauth and not access_token:
        raise ValueError(
            "Wrike credentials required: pass access_token (or set WRIKE_ACCESS_TOKEN), "
            "or client_id + client_secret + token_path for OAuth."
        )

    def build_client() -> WrikeClient:
        if client is not None:
            return client
        if use_oauth:
            # Refreshed when the sync runs, not when the source is built: each
            # refresh rotates the token, so building a source must not spend one.
            token, oauth_host = refresh_access_token(client_id, client_secret, token_path)
            return WrikeClient(token, oauth_host)
        return WrikeClient(access_token, host)

    @dlt.resource(
        name=TABLE_NAME,
        write_disposition="merge",
        primary_key="id",
        # Rows with _deleted=True are removed from dlt's table on merge, and
        # cognee's orphan cleanup then forgets the document (used from step 4 on).
        columns={"_deleted": {"data_type": "bool", "hard_delete": True}},
    )
    def wrike_items():
        yield from _iter_rows(build_client(), folder_ids)

    resource = wrike_items()
    # Opt into cognee's document path: each row is prose for normal cognify.
    setattr(resource, DOCUMENT_SOURCE_ATTR, SOURCE_TAG)
    return resource
