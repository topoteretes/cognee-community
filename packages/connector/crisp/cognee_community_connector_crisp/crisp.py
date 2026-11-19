"""Crisp connector for cognee: a dlt resource that syncs a website's conversations into memory.

Each conversation (visitor and operator messages, and optionally internal notes) becomes
one document, merged on the session id, and is meant to be handed to ``cognee.remember``::

    await cognee.remember(
        crisp_source(website_id="...", identifier="...", key="..."),
        dataset_name="crisp",
        primary_key="id",
        write_disposition="merge",  # required, the add pipeline defaults to "replace"
        max_rows_per_table=0,
    )
"""

import base64
import copy
import hashlib
import http.client
import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Iterator
from datetime import datetime, timezone
from typing import Any

from cognee.shared.logging_utils import get_logger
from cognee.tasks.ingestion import dlt_utils
from cognee.tasks.ingestion.dlt_utils import DOCUMENT_SOURCE_ATTR, PIPELINE_SCOPE_ATTR

logger = get_logger("crisp_connector")

CRISP_SOURCE_NAME = "crisp"
API_URL = "https://api.crisp.chat/v1"
_UUID = r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
_WEBSITE_RE = re.compile(rf"^{_UUID}$")
_SESSION_RE = re.compile(rf"^session_{_UUID}$")
# dlt snake-cases table names and collapses repeated underscores, so only names it
# leaves unchanged are accepted: single underscores between letters and digits.
_RESOURCE_NAME_RE = re.compile(r"^[a-z][a-z0-9]*(?:_[a-z0-9]+)*$")
_RESOURCE_NAME_MAX = 51
# Pages of the conversation list; Crisp returns at most this many a page.
_LIST_PAGE = 50
# A conversation list that keeps going past this many pages is treated as broken.
_MAX_LIST_PAGES = 2000
_MAX_RETRIES = 5
# cognee holds a process-wide staging lock while a dlt source runs, so a longer wait
# is not slept through: the run stops cleanly and the next one resumes.
_MAX_RETRY_AFTER = 30.0
_TITLE_MAX = 80
# Crisp reports no request counters, only how close each limit is
# (``x-rate-limit-watermarks: g=low;u=low;r=low``). When one reads ``high``, the client
# pauses this long before the next request.
_HIGH_WATERMARK_WAIT = 10.0
# Visitor details a document can carry. Email, phone and IP are personal data and only
# included when asked for.
VISITOR_FIELDS = ("nickname", "segments", "country", "city", "email", "phone", "ip")
DEFAULT_VISITOR_FIELDS = ("nickname", "segments")
_DAY_MS = 24 * 3600 * 1000
_COLUMNS = {
    "_deleted": {"data_type": "bool", "hard_delete": True},
    "title": {"data_type": "text", "nullable": True},
    "content": {"data_type": "text", "nullable": True},
    "url": {"data_type": "text", "nullable": True},
    **dlt_utils.NODE_SET_COLUMN_HINT,
}


class CrispAPIError(RuntimeError):
    """A Crisp API call failed. ``status`` is the HTTP status when there was one."""

    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


class CrispAuthError(CrispAPIError):
    """Crisp rejected the token (HTTP 401 or 403)."""


class CrispRateLimitError(CrispAPIError):
    """Still rate limited after the retries, or the run's request budget is used up."""

    def __init__(self, message: str):
        super().__init__(message, 429)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    # urllib keeps the Authorization header on a redirect to another host, so a
    # redirect is never followed: it surfaces as the 3xx HTTPError instead.
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _endpoint(path: str) -> str:
    """The request path with ids replaced, for errors and logs."""
    path = re.sub(rf"(session_)?{_UUID}", "{id}", path)
    return re.sub(r"/[0-9]+(?=/|$)", "/{n}", path)


def _secret(value: Any, name: str) -> str:
    text = value.strip() if isinstance(value, str) else ""
    if not text or any(ch.isspace() or ord(ch) < 32 for ch in text):
        raise ValueError(f"Pass {name}=, from your Crisp token (no spaces).")
    return text


def _retry_after(headers) -> float | None:
    try:
        return max(0.0, float(headers.get("Retry-After")))
    except (TypeError, ValueError):
        return None


def _backoff(attempt: int) -> float:
    return float(min(2**attempt, 16))


def _watermark_high(headers) -> bool:
    """True when ``x-rate-limit-watermarks`` reports any limit as ``high``."""
    try:
        value = headers.get("x-rate-limit-watermarks") or ""
    except AttributeError:
        return False
    levels = [part.split("=", 1)[-1].strip().lower() for part in str(value).split(";")]
    return "high" in levels


# ---------------------------------------------------------------------------
# HTTP client (stdlib only)
# ---------------------------------------------------------------------------
class CrispClient:
    """Minimal Crisp REST client: ``request(method, path, params=)`` returns the ``data``.

    Authenticates with a token's identifier and key (HTTP Basic) and the token's tier
    (``website`` or ``plugin``). ``HEAD`` returns the HTTP status instead of data.
    Retries 420/429 (honouring ``Retry-After`` up to 30 s), 5xx and dropped connections,
    and pauses when ``x-rate-limit-watermarks`` reports a limit as ``high``.
    Requests only go to ``api.crisp.chat``, and redirects are not followed.
    """

    def __init__(
        self,
        identifier: str,
        key: str,
        *,
        tier: str = "website",
        timeout: float = 60.0,
        sleep: Callable[[float], None] = time.sleep,
        check_active: Callable[[], None] | None = None,
    ):
        pair = f"{_secret(identifier, 'identifier')}:{_secret(key, 'key')}"
        self._auth = "Basic " + base64.b64encode(pair.encode()).decode()
        if tier not in ("website", "plugin"):
            raise ValueError("tier must be 'website' or 'plugin', the tier of your Crisp token.")
        self._tier = tier
        self._timeout = timeout
        self._sleep = sleep
        self._check_active = check_active
        self._opener = urllib.request.build_opener(_NoRedirect)

    def __repr__(self) -> str:
        # The key is a password: keep it out of any repr, log or traceback.
        return f"CrispClient(tier={self._tier!r}, token=<redacted>)"

    def _open(self, request):
        return self._opener.open(request, timeout=self._timeout)

    def _wait(self, seconds: float) -> None:
        if self._check_active is not None:
            self._check_active()
        self._sleep(seconds)

    def request(self, method: str, path: str, params: dict | None = None) -> Any:
        if not path.startswith("/"):
            raise ValueError("Crisp paths start with '/'.")
        url = f"{API_URL}{path}"
        if params:
            url += "?" + urllib.parse.urlencode(params)
        endpoint = _endpoint(path)
        for attempt in range(_MAX_RETRIES):
            request = urllib.request.Request(
                url,
                method=method,
                headers={
                    "Authorization": self._auth,
                    "X-Crisp-Tier": self._tier,
                    "Accept": "application/json",
                },
            )
            try:
                with self._open(request) as response:
                    raw = response.read()
                    status = getattr(response, "status", 200)
                    high = _watermark_high(getattr(response, "headers", None))
            except urllib.error.HTTPError as exc:
                status = exc.code
                if method == "HEAD" and status == 404:
                    return 404
                if status in (401, 403):
                    raise CrispAuthError(
                        f"Crisp rejected the token (HTTP {status}) for {endpoint}. Check the "
                        "identifier, key, tier and that the token can read this website.",
                        status,
                    ) from None
                if 300 <= status < 400:
                    raise CrispAPIError(
                        f"Crisp redirected {endpoint} (HTTP {status}); it is not followed.",
                        status,
                    ) from None
                limited = status in (420, 429)
                if limited:
                    delay = _retry_after(exc.headers)
                    if (
                        delay is not None and delay > _MAX_RETRY_AFTER
                    ) or attempt == _MAX_RETRIES - 1:
                        raise CrispRateLimitError(f"Crisp rate limited {endpoint}.") from None
                if (limited or status >= 500) and attempt < _MAX_RETRIES - 1:
                    delay = _retry_after(exc.headers) if limited else None
                    delay = delay if delay is not None else _backoff(attempt)
                    logger.warning(
                        "Crisp: HTTP %s on %s, retrying in %.0fs (%d/%d).",
                        status,
                        endpoint,
                        delay,
                        attempt + 1,
                        _MAX_RETRIES,
                    )
                    self._wait(delay)
                    continue
                raise CrispAPIError(
                    f"Crisp returned HTTP {status} for {endpoint}.", status
                ) from None
            except (OSError, http.client.HTTPException) as exc:
                if attempt < _MAX_RETRIES - 1:
                    self._wait(_backoff(attempt))
                    continue
                raise CrispAPIError(
                    f"Crisp could not be reached ({type(exc).__name__}) for {endpoint}."
                ) from None
            if high:
                logger.warning(
                    "Crisp: a rate limit watermark reads high; pausing %.0fs.",
                    _HIGH_WATERMARK_WAIT,
                )
                self._wait(_HIGH_WATERMARK_WAIT)
            if method == "HEAD":
                return status
            try:
                payload = json.loads(raw)
            except ValueError:
                payload = None
            if not isinstance(payload, dict) or payload.get("error") is True:
                raise CrispAPIError(f"Crisp returned no data for {endpoint}.", status)
            return payload.get("data")


class _CheckedClient:
    """Calls the host's ``check_active`` before every request of an injected client."""

    def __init__(self, client: Any, check_active: Callable[[], None]):
        self._client = client
        self._check_active = check_active

    def request(self, method: str, path: str, params: dict | None = None) -> Any:
        self._check_active()
        return self._client.request(method, path, params)


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------
def _when(ms: Any) -> str:
    try:
        return datetime.fromtimestamp(int(ms) / 1000, tz=timezone.utc).strftime(
            "%Y-%m-%d %H:%M UTC"
        )
    except (TypeError, ValueError, OverflowError, OSError):
        return "unknown time"


def _clean_name(value: Any) -> str:
    name = str(value or "").strip()
    return "" if "@" in name or name.startswith("+") else name


def _text(message: dict) -> str:
    """A message's readable text, or ``""`` when it has none."""
    content = message.get("content")
    kind = message.get("type")
    if kind in ("text", "note"):
        return str(content or "").strip()
    if kind == "file" and isinstance(content, dict):
        return f"(file: {content.get('name') or 'unnamed'})"
    if kind == "picker" and isinstance(content, dict):
        chosen = [
            c.get("label")
            for c in content.get("choices") or []
            if isinstance(c, dict) and c.get("selected")
        ]
        text = str(content.get("text") or "").strip()
        return " ".join(
            p for p in (text, f"(chose: {', '.join(map(str, chosen))})" if chosen else "") if p
        )
    if kind == "field" and isinstance(content, dict):
        return str(content.get("text") or "").strip()
    if isinstance(content, str):
        return content.strip()
    return ""


def _visitor_details(meta: dict, fields: tuple[str, ...]) -> list[tuple[str, str]]:
    """The selected visitor details, as (label, value) pairs, in a fixed order."""
    geo = (
        meta.get("device", {}).get("geolocation", {})
        if isinstance(meta.get("device"), dict)
        else {}
    )
    geo = geo if isinstance(geo, dict) else {}
    found = {
        "nickname": _clean_name(meta.get("nickname")),
        "segments": ", ".join(sorted(str(s) for s in meta.get("segments") or [] if s)),
        "country": str(geo.get("country") or "").strip(),
        "city": str(geo.get("city") or "").strip(),
        "email": str(meta.get("email") or "").strip(),
        "phone": str(meta.get("phone") or "").strip(),
        "ip": str(meta.get("ip") or "").strip(),
    }
    labels = {
        "nickname": "Visitor",
        "segments": "Segments",
        "country": "Country",
        "city": "City",
        "email": "Email",
        "phone": "Phone",
        "ip": "IP",
    }
    return [(labels[f], found[f]) for f in VISITOR_FIELDS if f in fields and found[f]]


def conversation_to_row(
    conversation: dict,
    messages: list[dict],
    website_id: str,
    *,
    include_notes: bool = True,
    visitor_fields: tuple[str, ...] = DEFAULT_VISITOR_FIELDS,
) -> dict[str, Any] | None:
    """Render a conversation as one deterministic document row, or ``None`` when it has
    no readable message.

    Messages are ordered by time and fingerprint, so the same conversation renders the
    same text whatever order Crisp sent it in. State changes and other ``event``
    messages are left out; edited messages appear in their current form. Only the
    ``visitor_fields`` asked for reach the header.
    """
    meta = conversation.get("meta") if isinstance(conversation.get("meta"), dict) else {}
    visitor = _clean_name(meta.get("nickname")) if "nickname" in visitor_fields else ""
    entries: list[tuple[int, str, str, str]] = []
    for m in messages:
        if not isinstance(m, dict) or m.get("type") == "event":
            continue
        if m.get("type") == "note" and not include_notes:
            continue
        text = _text(m)
        if not text:
            continue
        user = m.get("user") if isinstance(m.get("user"), dict) else {}
        name = _clean_name(user.get("nickname"))
        if m.get("type") == "note":
            label = f"Internal note ({name})" if name else "Internal note"
        elif m.get("from") == "operator":
            label = f"Operator ({name})" if name else "Operator"
        else:
            who = visitor or (name if "nickname" in visitor_fields else "")
            label = f"Visitor ({who})" if who else "Visitor"
        entries.append((int(m.get("timestamp") or 0), str(m.get("fingerprint")), label, text))
    if not entries:
        return None
    subject = str(meta.get("subject") or "").strip()
    heading = subject or (f"Conversation with {visitor}" if visitor else "Conversation")
    lines = [heading, f"State: {conversation.get('state') or 'unknown'}"]
    lines += [f"{label}: {value}" for label, value in _visitor_details(meta, visitor_fields)]
    lines.append("")
    for when, _fp, who, text in sorted(entries):
        lines.append(f"--- {who}, {_when(when)}")
        lines.append(text)
        lines.append("")
    session = conversation["session_id"]
    first = heading.splitlines()[0]
    return {
        "id": session,
        "title": first if len(first) <= _TITLE_MAX else first[: _TITLE_MAX - 3] + "...",
        "content": "\n".join(lines).strip(),
        "url": f"https://app.crisp.chat/website/{website_id}/inbox/{session}/",
        "_deleted": False,
    }


def deleted_row(session_id: str) -> dict[str, Any]:
    return {"id": session_id, "_deleted": True}


def _fingerprint(row: dict) -> str:
    return hashlib.sha256(json.dumps(row, sort_keys=True).encode()).hexdigest()[:16]


def _stamp(conversation: dict, visitor_fields: tuple[str, ...] = DEFAULT_VISITOR_FIELDS) -> str:
    """What the list shows of a conversation: updated_at, state, subject and the selected
    visitor details."""
    meta = conversation.get("meta") if isinstance(conversation.get("meta"), dict) else {}
    seen = {
        "u": conversation.get("updated_at"),
        "s": conversation.get("state"),
        "n": meta.get("nickname"),
        "j": meta.get("subject"),
        "v": _visitor_details(meta, visitor_fields),
    }
    return hashlib.sha256(json.dumps(seen, sort_keys=True).encode()).hexdigest()[:12]


# ---------------------------------------------------------------------------
# Sync
# ---------------------------------------------------------------------------
class _Run:
    """One sync run, counting requests against the run's budget."""

    def __init__(self, client: Any, website_id: str, budget: int):
        self.client = client
        self.website = website_id
        self.left = budget

    def call(self, method: str, path: str, params: dict | None = None) -> Any:
        if self.left <= 0:
            raise CrispRateLimitError("The run's Crisp request budget is used up.")
        self.left -= 1
        return self.client.request(method, path, params)

    def conversations(self) -> dict[str, dict]:
        """The full conversation list, empty conversations included."""
        found: dict[str, dict] = {}
        for page in range(1, _MAX_LIST_PAGES + 1):
            data = self.call(
                "GET",
                f"/website/{self.website}/conversations/{page}",
                {"include_empty": 1, "per_page": _LIST_PAGE},
            )
            if not isinstance(data, list):
                raise CrispAPIError("Crisp sent no conversation list.")
            fresh = 0
            for c in data:
                sid = c.get("session_id") if isinstance(c, dict) else None
                if isinstance(sid, str) and _SESSION_RE.match(sid) and sid not in found:
                    found[sid] = c
                    fresh += 1
            if not data:
                return found
            if not fresh:
                raise CrispAPIError("Crisp conversation pages did not move forward; stopping.")
        raise CrispAPIError("The Crisp conversation list did not end.")

    def messages(self, session_id: str) -> list[dict]:
        """Every message of a conversation, paging back with ``timestamp_before``."""
        path = f"/website/{self.website}/conversation/{session_id}/messages"
        found: dict[str, dict] = {}
        before = None
        while True:
            data = self.call("GET", path, {"timestamp_before": before} if before else None)
            if not isinstance(data, list):
                raise CrispAPIError("Crisp sent no message list.")
            fresh = [
                m for m in data if isinstance(m, dict) and str(m.get("fingerprint")) not in found
            ]
            for m in fresh:
                found[str(m.get("fingerprint"))] = m
            stamps = [int(m.get("timestamp") or 0) for m in data if isinstance(m, dict)]
            if not fresh or not stamps:
                return list(found.values())
            oldest = min(stamps)
            if before is not None and oldest >= before:
                return list(found.values())
            before = oldest

    def exists(self, session_id: str) -> bool:
        return self.call("HEAD", f"/website/{self.website}/conversation/{session_id}") != 404


def _check_owner(state: dict, website_id: str) -> None:
    if state.get("website") not in (None, website_id):
        raise ValueError(
            "This sync state belongs to another Crisp website. Give each website its own "
            "resource_name."
        )


def sync_website(
    client: Any,
    state: dict,
    website_id: str,
    stats: dict[str, int],
    *,
    include_notes: bool = True,
    visitor_fields: tuple[str, ...] = DEFAULT_VISITOR_FIELDS,
    recent_days: int = 3,
    rotation: int = 50,
    max_requests: int = 2000,
    now_ms: Callable[[], int] = lambda: int(time.time() * 1000),
) -> Iterator[dict[str, Any]]:
    """Yield changed conversations and tombstones for one Crisp website.

    Every run reads the full conversation list. A conversation's history is read again
    when its ``updated_at``, state or visitor details changed, and, because editing or
    removing an old message moves none of them, also when it was active in the last
    ``recent_days`` days, plus ``rotation`` older ones each run in turn. Unchanged
    conversations are skipped by fingerprint. A conversation missing from the list that
    answers 404 is forgotten. The run stops cleanly when its request budget or a rate
    limit runs out, and the next run resumes. The state is written after the last row,
    and dlt keeps it only once the load succeeded.
    """
    for key in ("scanned", "deleted", "skipped", "failed"):
        stats.setdefault(key, 0)
    _check_owner(state, website_id)
    known: dict[str, dict] = copy.deepcopy(state.get("conversations", {}))
    visitor_fields = tuple(f for f in VISITOR_FIELDS if f in visitor_fields)
    render = {"notes": include_notes, "visitor": list(visitor_fields)}
    rerender = state.get("render") not in (None, render)
    rotate_from = str(state.get("rotate_from") or "")
    run = _Run(client, website_id, max_requests)
    rows: list[dict] = []
    forget: set[str] = set()
    stopped = None
    next_rotate = rotate_from

    def read(sid: str, conversation: dict) -> None:
        try:
            messages = run.messages(sid)
        except CrispAPIError as exc:
            # Removed after the list was read: skipped, not forgotten. The next list no
            # longer has it, and its HEAD 404 settles it then.
            if exc.status != 404:
                raise
            logger.warning("Crisp: a conversation was removed while syncing (HTTP 404).")
            return
        row = conversation_to_row(
            conversation,
            messages,
            website_id,
            include_notes=include_notes,
            visitor_fields=visitor_fields,
        )
        stamp = _stamp(conversation, visitor_fields)
        if row is None:
            # Nothing readable left (every message removed, or only state events).
            if sid in known:
                forget.add(sid)
                known.pop(sid, None)
            return
        fingerprint = _fingerprint(row)
        saved = known.get(sid)
        known[sid] = {"t": stamp, "f": fingerprint}
        if saved and saved.get("f") == fingerprint:
            stats["skipped"] += 1
            return
        rows.append(row)

    try:
        listed = run.conversations()
        now = now_ms()
        changed = [
            sid
            for sid in sorted(listed)
            if rerender
            or sid not in known
            or known[sid].get("t") != _stamp(listed[sid], visitor_fields)
        ]
        recent = [
            sid
            for sid in sorted(listed)
            if sid not in changed
            and int(listed[sid].get("updated_at") or 0) >= now - recent_days * _DAY_MS
        ]
        older = [sid for sid in sorted(listed) if sid not in changed and sid not in recent]
        start = next((i for i, sid in enumerate(older) if sid > rotate_from), 0)
        batch = (older[start:] + older[:start])[: max(0, rotation)]

        for sid in changed + recent:
            read(sid, listed[sid])
        for sid in batch:
            read(sid, listed[sid])
            next_rotate = sid

        # A stored conversation missing from the complete list: only a 404 forgets it.
        for sid in sorted(set(known) - set(listed)):
            if not run.exists(sid):
                forget.add(sid)
    except CrispRateLimitError:
        stopped = "rate_limit"
        logger.warning("Crisp: rate limited or out of budget; stopping, the next sync resumes.")

    stats["failed"] = 1 if stopped else 0
    if stopped:
        stats[f"failed_{stopped}"] = 1
    for row in rows:
        stats["scanned"] += 1
        yield row
    for sid in sorted(forget):
        known.pop(sid, None)
        stats["deleted"] += 1
        yield deleted_row(sid)
    # Only reached once every row is out.
    state["website"] = website_id
    state["conversations"] = known
    state["rotate_from"] = next_rotate
    if not stopped:
        state["render"] = render


# ---------------------------------------------------------------------------
# Public factory
# ---------------------------------------------------------------------------
def crisp_source(
    *,
    website_id: str,
    identifier: str | None = None,
    key: str | None = None,
    tier: str = "website",
    include_notes: bool = True,
    visitor_fields: list[str] | tuple[str, ...] = DEFAULT_VISITOR_FIELDS,
    recent_days: int = 3,
    rotation: int = 50,
    max_requests: int = 2000,
    resource_name: str = "crisp",
    check_active: Callable[[], None] | None = None,
    client: Any = None,
    service: Any = None,
):
    """Return a ``dlt`` resource that yields a Crisp website's conversations for ``remember``.

    Args:
        website_id: The website's id (Settings, Website settings, Setup instructions).
        identifier, key: A Crisp token's identifier and key; see the README.
        tier: The token's tier, ``website`` (default) or ``plugin``.
        include_notes: Include internal notes, labelled ``Internal note``. Switching it
            re-renders every conversation.
        visitor_fields: Visitor details put in each document's header, out of
            ``nickname``, ``segments``, ``country``, ``city``, ``email``, ``phone`` and
            ``ip``. Default: nickname and segments. Email, phone and IP are personal data
            and only included when listed. Changing it re-renders every conversation.
        recent_days: Conversations active in this many days are read again every run,
            so an edited or removed message there is picked up on the next sync.
        rotation: Older conversations read again per run, in turn, to pick up late edits
            and removals; ``0`` turns it off.
        max_requests: Requests one run may make; the run stops cleanly past it and the
            next one resumes. Crisp allows a website token 10,000 a day.
        resource_name: Table name and key of the sync state: lowercase letters and
            digits with single underscores, at most 51 characters. Give each website its
            own. Keep it fixed across runs.
        check_active: Optional host authorization checkpoint, called before every API
            request, before every retry wait and around every row; raising stops the sync.
        client: Object with ``request(method, path, params)`` returning ``data`` (or the
            status for ``HEAD``); ``service`` is an alias.

    Returns:
        A ``dlt`` resource with ``primary_key="id"``, ``write_disposition="merge"`` and
        an ``_deleted`` hard-delete column. After a run, ``resource.cognee_sync_stats``
        holds counts only: ``scanned``, ``deleted``, ``skipped``, ``failed``, plus
        ``failed_rate_limit``.
    """
    import dlt

    if getattr(dlt_utils, "DOCUMENT_SYNC_VERSION", 0) < 2:
        raise RuntimeError(
            "The Crisp connector requires cognee 1.6.3 or later (table-scoped document "
            "cleanup). Upgrade cognee before syncing."
        )
    if client is not None and service is not None and client is not service:
        raise ValueError("Pass client= or service=, not both.")
    client = client if client is not None else service
    if not isinstance(website_id, str) or not _WEBSITE_RE.match(website_id.strip().lower()):
        raise ValueError("website_id must be your Crisp website id, a UUID (see the README).")
    website_id = website_id.strip().lower()
    if isinstance(visitor_fields, str) or not all(
        f in VISITOR_FIELDS for f in visitor_fields or ()
    ):
        raise ValueError(f"visitor_fields must be a list of: {', '.join(VISITOR_FIELDS)}.")
    visitor_fields = tuple(f for f in VISITOR_FIELDS if f in (visitor_fields or ()))
    for name, value in (
        ("recent_days", recent_days),
        ("rotation", rotation),
        ("max_requests", max_requests),
    ):
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            raise ValueError(f"{name} must be a whole number of 0 or more.")
    if max_requests < 10:
        raise ValueError("max_requests must be at least 10.")
    if (
        not isinstance(resource_name, str)
        or len(resource_name) > _RESOURCE_NAME_MAX
        or not _RESOURCE_NAME_RE.match(resource_name)
    ):
        raise ValueError(
            "resource_name must be lowercase letters and digits with single underscores, "
            "starting with a letter and at most 51 characters, so dlt uses it unchanged as "
            "the table name."
        )
    if client is None:
        client = CrispClient(identifier, key, tier=tier, check_active=check_active)
    if check_active is not None:
        client = _CheckedClient(client, check_active)
    stats: dict[str, int] = {}

    @dlt.resource(
        name=resource_name,
        primary_key="id",
        write_disposition="merge",
        columns=_COLUMNS,
    )
    def crisp_conversations():
        stats.clear()
        rows = sync_website(
            client,
            dlt.current.resource_state(),
            website_id,
            stats,
            include_notes=include_notes,
            visitor_fields=visitor_fields,
            recent_days=recent_days,
            rotation=rotation,
            max_requests=max_requests,
        )
        yield from dlt_utils.guarded_rows(rows, check_active)

    resource = crisp_conversations()
    # Conversations are prose: route them through cognify (document mode).
    setattr(resource, DOCUMENT_SOURCE_ATTR, CRISP_SOURCE_NAME)
    # Keep the sync state per resource_name (and, in cognee, per dataset).
    setattr(resource, PIPELINE_SCOPE_ATTR, resource_name)
    resource.cognee_sync_stats = stats
    return resource
