"""Zoho Desk connector for cognee: a dlt resource that syncs tickets and articles into memory.

Each ticket (with its conversation: customer and agent threads, and comments) and each
published knowledge-base article becomes one document, and is meant to be handed to
``cognee.remember``::

    await cognee.remember(
        zoho_desk_source(
            data_center="com",
            client_id="...", client_secret="...", refresh_token="...",
        ),
        dataset_name="zoho_desk",
        primary_key="id",
        write_disposition="merge",  # required, the add pipeline defaults to "replace"
        max_rows_per_table=0,
    )
"""

import copy
import hashlib
import html
import http.client
import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Iterator
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any

from cognee.shared.logging_utils import get_logger
from cognee.tasks.ingestion import dlt_utils
from cognee.tasks.ingestion.dlt_utils import DOCUMENT_SOURCE_ATTR, PIPELINE_SCOPE_ATTR

logger = get_logger("zoho_desk_connector")

ZOHO_DESK_SOURCE_NAME = "zoho_desk"
# Zoho runs each data center on its own domains: accounts.zoho.<dc> and desk.zoho.<dc>.
DATA_CENTERS = ("com", "eu", "in", "com.au", "jp", "ca", "sa", "com.cn")
_ID_RE = re.compile(r"^[0-9]{1,30}$")
# dlt snake-cases table names and collapses repeated underscores, so only names it
# leaves unchanged are accepted: single underscores between letters and digits.
_RESOURCE_NAME_RE = re.compile(r"^[a-z][a-z0-9]*(?:_[a-z0-9]+)*$")
_RESOURCE_NAME_MAX = 51
# Zoho Desk listings return at most 50 records a page (more is refused or capped).
_PAGE_SIZE = 50
# The Search API is an index that can lag behind a change by a little, so each window
# starts this much before the cursor; fingerprints skip what was already written.
_OVERLAP_MS = 10 * 60 * 1000
# A change search that keeps paging past this many tickets is replaced by a full read.
_SEARCH_MAX = 2000
# Deleted tickets and articles stay in the Recycle Bin for 60 days. A sync after a
# longer gap could miss a delete, so a full read reconciles the stored ids after this.
_RECONCILE_AFTER_MS = 50 * 24 * 3600 * 1000
# Stop cleanly when the day's API credits fall this low, leaving some for other apps.
_MIN_CREDITS = 50
_MAX_RETRIES = 5
# cognee holds a process-wide staging lock while a dlt source runs, so a longer wait
# is not slept through: the run stops cleanly and the next one resumes.
_MAX_RETRY_AFTER = 30.0
_TITLE_MAX = 80
_COLUMNS = {
    "_deleted": {"data_type": "bool", "hard_delete": True},
    "title": {"data_type": "text", "nullable": True},
    "content": {"data_type": "text", "nullable": True},
    "url": {"data_type": "text", "nullable": True},
    **dlt_utils.NODE_SET_COLUMN_HINT,
}


class ZohoDeskAPIError(RuntimeError):
    """A Zoho Desk API call failed. ``status`` is the HTTP status when there was one."""

    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


class ZohoDeskAuthError(ZohoDeskAPIError):
    """Zoho refused the OAuth client or refresh token, or the token lost access."""


class ZohoDeskRateLimitError(ZohoDeskAPIError):
    """Still rate limited after the retries, or the day's API credits are nearly used up."""

    def __init__(self, message: str):
        super().__init__(message, 429)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    # urllib keeps the Authorization header on a redirect to another host, so a
    # redirect is never followed: it surfaces as the 3xx HTTPError instead.
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _endpoint(path: str) -> str:
    """The request path with ids replaced, for errors and logs."""
    return re.sub(r"/[0-9]+(?=/|$)", "/{id}", path)


def _secret(value: Any, name: str) -> str:
    """A credential without surrounding whitespace, refused if it holds any inside.

    The message never echoes the value.
    """
    text = value.strip() if isinstance(value, str) else ""
    if not text or any(ch.isspace() or ord(ch) < 32 for ch in text):
        raise ValueError(f"Pass {name}= (no spaces); see the README for the Self Client setup.")
    return text


def _data_center(value: Any) -> str:
    dc = str(value or "").strip().lower().lstrip(".")
    if dc not in DATA_CENTERS:
        raise ValueError(
            f"data_center must be one of {', '.join(DATA_CENTERS)}: the end of your Zoho "
            "address (desk.zoho.com is 'com', desk.zoho.in is 'in')."
        )
    return dc


def _retry_after(headers) -> float | None:
    try:
        return max(0.0, float(headers.get("Retry-After")))
    except (TypeError, ValueError):
        return None


def _backoff(attempt: int) -> float:
    return float(min(2**attempt, 16))


def _date_ms(value: str | None) -> int | None:
    try:
        return int(parsedate_to_datetime(value).timestamp() * 1000) if value else None
    except (TypeError, ValueError):
        return None


def _ms(value: Any) -> int:
    """Milliseconds of a Zoho time like ``2026-10-19T09:53:10.000Z`` (0 if unreadable)."""
    try:
        return int(datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp() * 1000)
    except (TypeError, ValueError):
        return 0


def _iso(ms: int) -> str:
    return (
        datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.")
        + f"{ms % 1000:03d}Z"
    )


# ---------------------------------------------------------------------------
# HTTP client (stdlib only)
# ---------------------------------------------------------------------------
class ZohoDeskClient:
    """Minimal Zoho Desk API client: ``get(path, params=)`` returns ``(payload, server_ms)``.

    Exchanges the Self Client refresh token for an access token, refreshes it when it
    expires or a call answers 401, and sends the ``orgId`` header. Retries 429
    (honouring ``Retry-After`` up to 30 s), 5xx and dropped connections. A 204 or an
    empty body is an empty list. Requests only go to ``accounts.zoho.<dc>`` and
    ``desk.zoho.<dc>``, and redirects are not followed. ``credits_left`` holds the
    last ``X-Rate-Limit-Remaining-v3`` header.
    """

    def __init__(
        self,
        *,
        data_center: str,
        client_id: str,
        client_secret: str,
        refresh_token: str,
        org_id: str | None = None,
        timeout: float = 60.0,
        sleep: Callable[[float], None] = time.sleep,
        check_active: Callable[[], None] | None = None,
    ):
        dc = _data_center(data_center)
        self.accounts_url = f"https://accounts.zoho.{dc}/oauth/v2/token"
        self.api_url = f"https://desk.zoho.{dc}/api/v1"
        self._client_id = _secret(client_id, "client_id")
        self._client_secret = _secret(client_secret, "client_secret")
        self._refresh_token = _secret(refresh_token, "refresh_token")
        if org_id is not None and not _ID_RE.match(str(org_id)):
            raise ValueError("org_id must be the numeric id of your Zoho Desk organization.")
        self.org_id = str(org_id) if org_id is not None else None
        self._timeout = timeout
        self._sleep = sleep
        self._check_active = check_active
        self._opener = urllib.request.build_opener(_NoRedirect)
        self._access: str | None = None
        self._expires = 0.0
        self.credits_left: int | None = None

    def __repr__(self) -> str:
        # The credentials are passwords: keep them out of any repr, log or traceback.
        return f"ZohoDeskClient(api_url={self.api_url!r}, credentials=<redacted>)"

    def _open(self, request):
        return self._opener.open(request, timeout=self._timeout)

    def _wait(self, seconds: float) -> None:
        if self._check_active is not None:
            self._check_active()
        self._sleep(seconds)

    def _refresh(self) -> None:
        body = urllib.parse.urlencode(
            {
                "grant_type": "refresh_token",
                "client_id": self._client_id,
                "client_secret": self._client_secret,
                "refresh_token": self._refresh_token,
            }
        ).encode()
        request = urllib.request.Request(self.accounts_url, data=body, method="POST")
        try:
            with self._open(request) as response:
                answer = json.loads(response.read() or b"{}")
        except urllib.error.HTTPError as exc:
            raise ZohoDeskAuthError(
                f"Zoho refused the token refresh (HTTP {exc.code}). Check the client id, "
                "secret, refresh token and data_center.",
                exc.code,
            ) from None
        except (OSError, http.client.HTTPException, ValueError):
            raise ZohoDeskAPIError("Zoho accounts could not be reached for a token.") from None
        token = answer.get("access_token") if isinstance(answer, dict) else None
        if not token:
            # Zoho answers 200 with {"error": "invalid_code"} for a bad refresh token.
            reason = answer.get("error") if isinstance(answer, dict) else None
            raise ZohoDeskAuthError(
                f"Zoho gave no access token ({reason or 'no reason given'}). Check the client "
                "id, secret, refresh token and data_center.",
                401,
            )
        self._access = str(token)
        self._expires = time.monotonic() + float(answer.get("expires_in") or 3600) - 60

    def get(self, path: str, params: dict | None = None) -> tuple[dict, int | None]:
        if not path.startswith("/"):
            raise ValueError("Zoho Desk paths start with '/'.")
        url = f"{self.api_url}{path}"
        if params:
            url += "?" + urllib.parse.urlencode(params)
        endpoint = _endpoint(path)
        refreshed = False
        for attempt in range(_MAX_RETRIES):
            if self._access is None or time.monotonic() >= self._expires:
                self._refresh()
            headers = {
                "Authorization": f"Zoho-oauthtoken {self._access}",
                "Accept": "application/json",
            }
            if self.org_id:
                headers["orgId"] = self.org_id
            request = urllib.request.Request(url, headers=headers)
            try:
                with self._open(request) as response:
                    raw = response.read()
                    status = getattr(response, "status", 200)
                    date = response.headers.get("Date")
                    credits = response.headers.get("X-Rate-Limit-Remaining-v3")
            except urllib.error.HTTPError as exc:
                status = exc.code
                if status == 401 and not refreshed:
                    # An access token can be revoked or expire early: refresh once.
                    refreshed, self._access = True, None
                    continue
                if status == 401:
                    raise ZohoDeskAuthError(
                        "Zoho Desk rejected the access token (HTTP 401). Check the scopes "
                        "and that the user can still read the organization.",
                        401,
                    ) from None
                if 300 <= status < 400:
                    raise ZohoDeskAPIError(
                        f"Zoho Desk redirected {endpoint} (HTTP {status}); it is not followed. "
                        "Check data_center.",
                        status,
                    ) from None
                if status == 429:
                    delay = _retry_after(exc.headers)
                    if (
                        delay is not None and delay > _MAX_RETRY_AFTER
                    ) or attempt == _MAX_RETRIES - 1:
                        raise ZohoDeskRateLimitError(
                            f"Zoho Desk rate limited {endpoint} (or the day's credits ran out)."
                        ) from None
                if (status == 429 or status >= 500) and attempt < _MAX_RETRIES - 1:
                    delay = _retry_after(exc.headers) if status == 429 else None
                    delay = delay if delay is not None else _backoff(attempt)
                    logger.warning(
                        "Zoho Desk: HTTP %s on %s, retrying in %.0fs (%d/%d).",
                        status,
                        endpoint,
                        delay,
                        attempt + 1,
                        _MAX_RETRIES,
                    )
                    self._wait(delay)
                    continue
                raise ZohoDeskAPIError(
                    f"Zoho Desk returned HTTP {status} for {endpoint}.", status
                ) from None
            except (OSError, http.client.HTTPException) as exc:
                if attempt < _MAX_RETRIES - 1:
                    self._wait(_backoff(attempt))
                    continue
                raise ZohoDeskAPIError(
                    f"Zoho Desk could not be reached ({type(exc).__name__}) for {endpoint}."
                ) from None
            if credits is not None and credits.strip().isdigit():
                self.credits_left = int(credits)
            if status == 204 or not raw.strip():
                return {}, _date_ms(date)  # Zoho answers 204 for an empty list
            try:
                payload = json.loads(raw)
            except ValueError:
                payload = None
            if not isinstance(payload, dict):
                raise ZohoDeskAPIError(
                    f"Zoho Desk returned HTTP {status} without a JSON object for {endpoint}.",
                    status,
                )
            return payload, _date_ms(date)
        raise ZohoDeskAuthError("Zoho Desk kept rejecting a fresh access token.", 401)


class _CheckedClient:
    """Calls the host's ``check_active`` before every request of an injected client."""

    def __init__(self, client: Any, check_active: Callable[[], None]):
        self._client = client
        self._check_active = check_active

    @property
    def credits_left(self) -> int | None:
        return getattr(self._client, "credits_left", None)

    @property
    def org_id(self) -> str | None:
        return getattr(self._client, "org_id", None)

    @org_id.setter
    def org_id(self, value: str | None) -> None:
        self._client.org_id = value

    def get(self, path: str, params: dict | None = None) -> tuple[dict, int | None]:
        self._check_active()
        return self._client.get(path, params)


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------
_TAG_RE = re.compile(r"<[^>]+>")
_BLOCK_RE = re.compile(r"<\s*(br|/p|/div|/li|/tr|/h[1-6]|/blockquote)\s*/?>", re.I)
_DROP_RE = re.compile(r"<(script|style)[^>]*>.*?</\1>", re.I | re.S)


def _plain(text: Any) -> str:
    """Readable text: scripts and tags dropped, entities decoded, blank runs collapsed."""
    text = str(text or "")
    if "<" in text:
        text = _TAG_RE.sub("", _BLOCK_RE.sub("\n", _DROP_RE.sub("", text)))
    text = html.unescape(text).replace("\xa0", " ")
    out: list[str] = []
    for line in text.replace("\r\n", "\n").split("\n"):
        line = line.strip()
        if line or (out and out[-1]):
            out.append(line)
    return "\n".join(out).strip()


def _when(value: Any) -> str:
    ms = _ms(value)
    if not ms:
        return "unknown time"
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def _name(who: Any) -> str:
    """A person's name, never their email address or phone number."""
    if not isinstance(who, dict):
        return ""
    name = str(who.get("name") or "").strip()
    if not name:
        name = " ".join(str(p) for p in (who.get("firstName"), who.get("lastName")) if p).strip()
    return "" if "@" in name or name.startswith("+") else name


def _label(kind: str, name: str) -> str:
    return f"{kind} ({name})" if name else kind


def _short_title(text: str) -> str:
    line = text.splitlines()[0] if text else ""
    return line if len(line) <= _TITLE_MAX else line[: _TITLE_MAX - 3] + "..."


def ticket_to_row(
    ticket: dict, threads: list[dict], comments: list[dict], department: str
) -> dict[str, Any]:
    """Render a ticket and its conversation as one deterministic document row.

    Threads and comments are ordered by time and id, so the same ticket renders the
    same text whatever order Zoho sent it in.
    """
    contact = _name(ticket.get("contact"))
    entries: list[tuple[int, str, str, str]] = []
    has_description = False
    for t in threads:
        if not isinstance(t, dict) or not t.get("id"):
            continue
        has_description = has_description or bool(t.get("isDescriptionThread"))
        author = _name(t.get("author"))
        if str(t.get("visibility") or "").lower() == "private":
            kind = "Internal thread"
        elif str(t.get("direction") or "").lower() == "in":
            kind, author = "Customer", author or contact
        else:
            kind = "Agent"
        text = _plain(t.get("content")) or _plain(t.get("summary")) or "(no text)"
        entries.append((_ms(t.get("createdTime")), str(t["id"]), _label(kind, author), text))
    for c in comments:
        if not isinstance(c, dict) or not c.get("id"):
            continue
        kind = "Comment" if c.get("isPublic") else "Internal comment"
        author = _name(c.get("commenter") or c.get("author"))
        text = _plain(c.get("content")) or "(no text)"
        entries.append(
            (
                _ms(c.get("commentedTime") or c.get("createdTime")),
                str(c["id"]),
                _label(kind, author),
                text,
            )
        )
    description = _plain(ticket.get("description"))
    if description and not has_description:
        entries.append(
            (_ms(ticket.get("createdTime")), "0", _label("Customer", contact), description)
        )

    subject = _plain(ticket.get("subject")) or "(no subject)"
    subject_line = subject.splitlines()[0]
    number = ticket.get("ticketNumber")
    heading = f"Ticket #{number}: {subject_line}" if number else f"Ticket: {subject_line}"
    details = [f"Status: {ticket.get('status') or 'unknown'}"]
    for label, key in (("Priority", "priority"), ("Category", "category"), ("Channel", "channel")):
        if ticket.get(key):
            details.append(f"{label}: {ticket[key]}")
    assignee = _name(ticket.get("assignee"))
    if assignee:
        details.append(f"Assignee: {assignee}")
    lines = [heading, f"Department: {department}", " | ".join(details)]
    if contact:
        lines.append(f"Customer: {contact}")
    lines.append("")
    for stamp, _id, who, text in sorted(entries):
        lines.append(f"--- {who}, {_when(_iso(stamp)) if stamp else 'unknown time'}")
        lines.append(text)
        lines.append("")
    row = {
        "id": f"ticket:{ticket['id']}",
        "title": _short_title(f"#{number} {subject_line}" if number else subject_line),
        "content": "\n".join(lines).strip(),
        "url": ticket.get("webUrl")
        if str(ticket.get("webUrl") or "").startswith("https://")
        else None,
        "_deleted": False,
    }
    if department:
        row[dlt_utils.NODE_SET_COLUMN] = [department]
    return row


def article_to_row(article: dict, department: str) -> dict[str, Any] | None:
    """Render a knowledge-base article, or ``None`` when it has no readable text."""
    title = _plain(article.get("title")) or "(untitled)"
    body = _plain(article.get("answer")) or _plain(article.get("summary"))
    if not body:
        return None
    lines = [f"Article: {title}", f"Department: {department}"]
    category = article.get("category")
    if isinstance(category, dict) and category.get("name"):
        lines.append(f"Category: {category['name']}")
    lines += ["", body]
    url = article.get("webUrl") or article.get("portalUrl")
    row = {
        "id": f"article:{article['id']}",
        "title": _short_title(title),
        "content": "\n".join(lines).strip(),
        "url": url if str(url or "").startswith("https://") else None,
        "_deleted": False,
    }
    if department:
        row[dlt_utils.NODE_SET_COLUMN] = [department]
    return row


def _live_article(article: dict) -> bool:
    """Published and not in the Knowledge Base's trash."""
    return str(article.get("status") or "").lower() == "published" and not article.get("isTrashed")


def deleted_row(row_id: str) -> dict[str, Any]:
    return {"id": row_id, "_deleted": True}


def _fingerprint(row: dict) -> str:
    return hashlib.sha256(json.dumps(row, sort_keys=True).encode()).hexdigest()[:16]


# ---------------------------------------------------------------------------
# Sync
# ---------------------------------------------------------------------------
class _Run:
    """One sync run: paging, lookups and the credit budget."""

    def __init__(self, client: Any, stats: dict[str, int]):
        self.client = client
        self.stats = stats
        self.first_index = 0
        self.server_ms: int | None = None

    def get(self, path: str, params: dict | None = None) -> dict:
        credits = getattr(self.client, "credits_left", None)
        if credits is not None and credits < _MIN_CREDITS:
            raise ZohoDeskRateLimitError("Zoho Desk API credits for today are nearly used up.")
        payload, server_ms = self.client.get(path, params)
        if server_ms and self.server_ms is None:
            self.server_ms = server_ms
        return payload

    def pages(
        self, path: str, params: dict | None = None, limit: int | None = None
    ) -> Iterator[dict]:
        """Every record of a ``from``/``limit`` listing (``data``), up to ``limit`` records."""
        start = self.first_index
        seen: set[str] = set()
        count = 0
        while True:
            query = {**(params or {}), "from": start, "limit": _PAGE_SIZE}
            try:
                payload = self.get(path, query)
            except ZohoDeskAPIError as exc:
                # Some listings count from 1: a 422 on from=0 means start at 1 instead.
                if exc.status == 422 and start == 0 and not seen:
                    self.first_index = start = 1
                    continue
                raise
            data = payload.get("data", [])
            if not isinstance(data, list):
                raise ZohoDeskAPIError(f"Zoho Desk sent no record list for {_endpoint(path)}.")
            fresh = 0
            for record in data:
                rid = str(record.get("id")) if isinstance(record, dict) else ""
                if rid and rid not in seen:
                    seen.add(rid)
                    fresh += 1
                    count += 1
                    yield record
            if len(data) < _PAGE_SIZE:
                return
            if not fresh:
                raise ZohoDeskAPIError(
                    f"Zoho Desk pages did not move forward for {_endpoint(path)}."
                )
            if limit is not None and count >= limit:
                raise _TooManyChangesError()
            start += _PAGE_SIZE

    def org(self) -> str:
        if getattr(self.client, "org_id", None):
            return str(self.client.org_id)
        orgs = self.get("/organizations").get("data", [])
        ids = [str(o.get("id")) for o in orgs if isinstance(o, dict) and o.get("id")]
        if len(ids) != 1:
            raise ValueError(
                f"The account belongs to {len(ids)} Zoho Desk organizations; pass org_id= "
                "(see the README)."
            )
        self.client.org_id = ids[0]
        return ids[0]

    def departments(self) -> dict[str, str]:
        return {
            str(d["id"]): str(d.get("name") or d["id"])
            for d in self.pages("/departments")
            if isinstance(d, dict) and d.get("id")
        }

    def ticket(self, ticket_id: str) -> dict | None:
        try:
            ticket = self.get(f"/tickets/{ticket_id}", {"include": "contacts,assignee"})
        except ZohoDeskAPIError as exc:
            if exc.status in (404, 422):
                return None
            raise
        return ticket if str(ticket.get("id")) == ticket_id else None

    def conversation(self, ticket_id: str, comments: bool) -> tuple[list[dict], list[dict]]:
        threads: list[dict] = []
        found: list[dict] = []
        for item in self.pages(f"/tickets/{ticket_id}/conversations"):
            kind = str(item.get("type") or "").lower()
            if kind == "thread":
                threads.append(item)
            elif kind == "comment" and comments:
                found.append(item)
        full = []
        for thread in threads:
            # The conversation listing only carries a thread's summary.
            tid = str(thread.get("id") or "")
            if not _ID_RE.match(tid):
                continue
            try:
                full.append({**thread, **self.get(f"/tickets/{ticket_id}/threads/{tid}")})
            except ZohoDeskAPIError as exc:
                if exc.status not in (404, 422):
                    raise
        return full, found

    def article(self, article_id: str) -> dict | None:
        try:
            article = self.get(f"/articles/{article_id}")
        except ZohoDeskAPIError as exc:
            if exc.status in (404, 422):
                return None
            raise
        return article if str(article.get("id")) == article_id else None


class _TooManyChangesError(Exception):
    """A change search ran past ``_SEARCH_MAX`` records."""


def _check_owner(state: dict, owner: str) -> None:
    """Refuse a sync state written for another Zoho Desk organization."""
    if state.get("owner") not in (None, owner):
        raise ValueError(
            "This sync state belongs to another Zoho Desk organization. Give each one its "
            "own resource_name."
        )


def sync_desk(
    client: Any,
    state: dict,
    stats: dict[str, int],
    *,
    departments: list[str] | None = None,
    include_comments: bool = True,
    include_articles: bool = True,
    now_ms: Callable[[], int] = lambda: int(time.time() * 1000),
) -> Iterator[dict[str, Any]]:
    """Yield changed tickets and articles, and tombstones, for the organization.

    The first run (and one after more than 50 days, when the Recycle Bin may have
    dropped deletes) reads every ticket; later runs ask the Search API for tickets
    modified since the cursor. Changed tickets are read again with their conversation.
    Deletes come from the Recycle Bin. A ticket that moves out of the selected
    departments is forgotten. Running out of API credits or a rate limit stops the run
    cleanly: what was read is kept and the cursor stays, so the next run resumes. The
    state is written after the last row, and dlt keeps it only once the load succeeded.
    """
    for key in ("scanned", "deleted", "skipped", "failed"):
        stats.setdefault(key, 0)
    run = _Run(client, stats)
    owner = state.get("owner")

    tickets: dict[str, dict] = copy.deepcopy(state.get("tickets", {}))
    articles: dict[str, dict] = copy.deepcopy(state.get("articles", {}))
    render = {"comments": include_comments, "departments": sorted(departments or [])}
    rerender = state.get("render") not in (None, render)
    selected = set(departments or [])
    rows: list[dict] = []
    forget: set[str] = set()
    stopped = None
    new_state: dict[str, Any] = {}

    def wanted(record: dict) -> bool:
        # Articles may carry no department; they are kept rather than guessed at.
        dept = record.get("departmentId")
        return not selected or dept is None or str(dept) in selected

    def unchanged(store: dict[str, dict], row_id: str, modified: Any) -> bool:
        saved = store.get(row_id)
        return bool(saved and not rerender and modified and saved.get("m") == modified)

    def emit(row_id: str, row: dict | None, store: dict[str, dict], modified: Any) -> None:
        if row is None:
            if row_id in store:
                forget.add(row_id)
            return
        fingerprint = _fingerprint(row)
        saved = store.get(row_id)
        store[row_id] = {"m": modified, "f": fingerprint}
        if saved and saved.get("f") == fingerprint:
            stats["skipped"] += 1
            return
        rows.append(row)

    try:
        owner = run.org()
        _check_owner(state, owner)
        names = run.departments()
        # The Recycle Bin holds deleted tickets for 60 days. It is read first: the
        # Search API can still return a ticket that was just moved there, and reading
        # that ticket's conversation answers 403.
        binned: set[str] = set()
        for item in run.pages("/recycleBin"):
            kind = str(item.get("type") or item.get("module") or "").lower()
            rid = str(item.get("id") or "")
            if "ticket" in kind:
                binned.add(f"ticket:{rid}")
        # The server's clock (Date header) keeps the cursor independent of this machine's.
        started = run.server_ms or now_ms()
        cursor = state.get("cursor")
        last_full = int(state.get("last_full") or 0)
        full = cursor is None or rerender or started - last_full > _RECONCILE_AFTER_MS
        changed: list[dict] = []
        if not full:
            window = f"{_iso(int(cursor) - _OVERLAP_MS)},{_iso(started + 60_000)}"
            try:
                changed = list(
                    run.pages(
                        "/tickets/search",
                        {"modifiedTimeRange": window, "sortBy": "modifiedTime"},
                        limit=_SEARCH_MAX,
                    )
                )
            except _TooManyChangesError:
                logger.warning(
                    "Zoho Desk: over %d changed tickets; reading every ticket.", _SEARCH_MAX
                )
                full = True
        if full:
            changed = list(
                run.pages(
                    "/tickets",
                    {"sortBy": "createdTime", "fields": "modifiedTime,departmentId,status"},
                )
            )
            listed = {f"ticket:{t.get('id')}" for t in changed}
            # A stored ticket missing from the listing (archived, say) is looked up on
            # its own: only one that is really gone is forgotten.
            for row_id in sorted(set(tickets) - listed):
                ticket = run.ticket(row_id.split(":", 1)[1])
                if ticket is None or not wanted(ticket):
                    forget.add(row_id)

        for record in changed:
            tid = str(record.get("id") or "")
            if not _ID_RE.match(tid):
                continue
            row_id = f"ticket:{tid}"
            if row_id in binned:
                continue
            if not wanted(record):
                if row_id in tickets:
                    forget.add(row_id)  # moved to a department that is not selected
                continue
            if unchanged(tickets, row_id, record.get("modifiedTime")):
                continue
            ticket = run.ticket(tid)
            if ticket is None or not wanted(ticket):
                emit(row_id, None, tickets, None)
                continue
            try:
                threads, comments = run.conversation(tid, include_comments)
            except ZohoDeskAPIError as exc:
                # A ticket deleted between the search and this read answers 403 or 404;
                # it is skipped, not forgotten, and the Recycle Bin settles it next run.
                if exc.status not in (403, 404, 422):
                    raise
                logger.warning("Zoho Desk: a ticket's conversation is gone (HTTP %s).", exc.status)
                continue
            department = names.get(str(ticket.get("departmentId")), "")
            row = ticket_to_row(ticket, threads, comments, department)
            emit(row_id, row, tickets, record.get("modifiedTime") or ticket.get("modifiedTime"))

        if include_articles:
            # A deleted article goes to the Knowledge Base's own trash, not the Recycle
            # Bin: it drops out of the article listing, and a lookup by id still answers
            # 200 with "isTrashed": true (checked on a Zoho Desk account). So the listing
            # is read in full every run (one request per 50 articles), and a stored
            # article missing from it is looked up on its own.
            listed: set[str] = set()
            for item in run.pages("/articles", {"sortBy": "-modifiedTime"}):
                aid = str(item.get("id") or "")
                if not _ID_RE.match(aid):
                    continue
                row_id = f"article:{aid}"
                listed.add(row_id)
                modified = item.get("modifiedTime")
                if not _live_article(item) or not wanted(item):
                    emit(row_id, None, articles, None)
                    continue
                if unchanged(articles, row_id, modified):
                    continue
                article = run.article(aid)
                if article is None or not _live_article(article) or not wanted(article):
                    emit(row_id, None, articles, None)
                    continue
                department = names.get(str(article.get("departmentId")), "")
                emit(row_id, article_to_row(article, department), articles, modified)
            for row_id in sorted(set(articles) - listed):
                article = run.article(row_id.split(":", 1)[1])
                if article is None or not _live_article(article) or not wanted(article):
                    forget.add(row_id)
        else:
            forget |= set(articles)

        # Deleted tickets were read from the Recycle Bin above.
        forget |= {r for r in binned if r in tickets}

        new_state = {
            "cursor": started,
            "last_full": started if full else last_full,
        }
    except ZohoDeskRateLimitError:
        stopped = "rate_limit"
        logger.warning(
            "Zoho Desk: rate limited or out of credits; stopping, the next sync resumes."
        )

    stats["failed"] = 1 if stopped else 0
    if stopped:
        stats[f"failed_{stopped}"] = 1

    for row in rows:
        stats["scanned"] += 1
        yield row
    for row_id in sorted(forget):
        tickets.pop(row_id, None)
        articles.pop(row_id, None)
        stats["deleted"] += 1
        yield deleted_row(row_id)
    # Only reached once every row is out. A stopped run keeps the old cursors.
    if owner is not None:
        state["owner"] = owner
    state["tickets"] = tickets
    state["articles"] = articles
    if not stopped:
        state["render"] = render
        state.update(new_state)


# ---------------------------------------------------------------------------
# Public factory
# ---------------------------------------------------------------------------
def zoho_desk_source(
    *,
    data_center: str = "com",
    client_id: str | None = None,
    client_secret: str | None = None,
    refresh_token: str | None = None,
    org_id: str | None = None,
    departments: list[str] | tuple[str, ...] | None = None,
    include_comments: bool = True,
    include_articles: bool = True,
    resource_name: str = "zoho_desk",
    check_active: Callable[[], None] | None = None,
    client: Any = None,
    service: Any = None,
):
    """Return a ``dlt`` resource that yields Zoho Desk tickets and articles for ``remember``.

    Args:
        data_center: Where the account lives: ``com``, ``eu``, ``in``, ``com.au``,
            ``jp``, ``ca``, ``sa`` or ``com.cn`` (the end of your Zoho address).
        client_id, client_secret, refresh_token: From a Zoho Self Client; see the README.
        org_id: The Zoho Desk organization id. Needed only when the account belongs to
            more than one organization.
        departments: Ids of the departments to sync; all by default. A ticket or
            article that moves out of them is forgotten.
        include_comments: Include ticket comments, labelled ``Internal comment`` (or
            ``Comment`` when public). Switching it re-reads every ticket.
        include_articles: Include published knowledge-base articles.
        resource_name: Table name and key of the sync state: lowercase letters and
            digits with single underscores, at most 51 characters. Give each
            organization its own. Keep it fixed across runs.
        check_active: Optional host authorization checkpoint, called before every API
            request, before every retry wait and around every row; raising stops the sync.
        client: Object with ``get(path, params)`` returning ``(payload, server_ms)``;
            ``service`` is an alias.

    Returns:
        A ``dlt`` resource with ``primary_key="id"``, ``write_disposition="merge"`` and
        an ``_deleted`` hard-delete column. Row ids are ``ticket:<id>`` and
        ``article:<id>``. After a run, ``resource.cognee_sync_stats`` holds counts only:
        ``scanned``, ``deleted``, ``skipped``, ``failed``, plus ``failed_rate_limit``.
    """
    import dlt

    if getattr(dlt_utils, "DOCUMENT_SYNC_VERSION", 0) < 2:
        raise RuntimeError(
            "The Zoho Desk connector requires cognee 1.6.3 or later (table-scoped document "
            "cleanup and per-row node sets). Upgrade cognee before syncing."
        )
    if client is not None and service is not None and client is not service:
        raise ValueError("Pass client= or service=, not both.")
    client = client if client is not None else service
    if departments is not None:
        if isinstance(departments, str):
            raise ValueError("departments must be a list of department ids, not one string.")
        departments = [str(d) for d in dict.fromkeys(departments)]
        if not departments or any(not _ID_RE.match(d) for d in departments):
            raise ValueError("departments must be a non-empty list of numeric department ids.")
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
        client = ZohoDeskClient(
            data_center=data_center,
            client_id=client_id,
            client_secret=client_secret,
            refresh_token=refresh_token,
            org_id=org_id,
            check_active=check_active,
        )
    if check_active is not None:
        client = _CheckedClient(client, check_active)
    stats: dict[str, int] = {}

    @dlt.resource(
        name=resource_name,
        primary_key="id",
        write_disposition="merge",
        columns=_COLUMNS,
    )
    def zoho_desk_records():
        stats.clear()
        rows = sync_desk(
            client,
            dlt.current.resource_state(),
            stats,
            departments=departments,
            include_comments=include_comments,
            include_articles=include_articles,
        )
        yield from dlt_utils.guarded_rows(rows, check_active)

    resource = zoho_desk_records()
    # Tickets and articles are prose: route them through cognify (document mode).
    setattr(resource, DOCUMENT_SOURCE_ATTR, ZOHO_DESK_SOURCE_NAME)
    # Keep the sync state per resource_name (and, in cognee, per dataset).
    setattr(resource, PIPELINE_SCOPE_ATTR, resource_name)
    resource.cognee_sync_stats = stats
    return resource
