"""Zendesk connector for cognee: a ``dlt`` source that turns support tickets and
Help Center articles into memory.

Hand the source to :func:`cognee.remember`::

    import cognee
    from cognee_community_connector_zendesk import zendesk_source

    await cognee.remember(
        zendesk_source(subdomain="acme", oauth_token=token),
        dataset_name="zendesk",
        primary_key="id",
        write_disposition="merge",   # required: the default "replace" wipes earlier tickets
        max_rows_per_table=0,        # compare forget-on-delete against every row
    )

Tickets come from the cursor-based incremental export, one document per ticket with
its comment thread; articles are listed in full each run. The README explains the
sync, the failure handling and what is read.
"""

from __future__ import annotations

import base64
import copy
import http.client
import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Iterator
from html.parser import HTMLParser
from typing import Any

from cognee.shared.logging_utils import get_logger
from cognee.tasks.ingestion import dlt_utils
from cognee.tasks.ingestion.dlt_utils import (
    DOCUMENT_SOURCE_ATTR,
    PIPELINE_SCOPE_ATTR,
    guarded_rows,
)

logger = get_logger("zendesk_connector")

ZENDESK_SOURCE_NAME = "zendesk"
_RESOURCES = ("tickets", "articles")
# One export page holds up to 1000 tickets. Each changed ticket also costs one comments
# request, so a single page keeps a run to roughly 1000 requests: a large first backfill
# saves its progress every run instead of starting over when a run fails or a short-lived
# OAuth token expires.
DEFAULT_EXPORT_PAGES_PER_RUN = 1
# Attempts per request: the first try plus four retries on 429, 5xx and dropped
# connections (429 waits for Retry-After).
_MAX_RETRIES = 5
# cognee holds a process-wide staging lock while a dlt source runs, so a longer
# Retry-After is not slept through: the run stops cleanly and the next one resumes.
_MAX_RETRY_AFTER = 30.0
# The largest page[size] the Help Center cursor pagination accepts.
_PAGE_SIZE = 100
# A ticket whose comments cannot be read (403, 422, or 5xx after the retries) is asked
# again on the next runs; after this many failed runs it is given up and left as stored.
_GIVE_UP_RUNS = 3
# Fresh 5xx answers from different tickets in a row that mean Zendesk itself is down:
# the run stops cleanly, keeping what it read and setting the rest aside for the next run.
_OUTAGE_STREAK = 3
# Runs in a row one resource (tickets or articles) may fail while the other still syncs.
# Raising in the same run would make pipeline.run fail and dlt would keep neither
# resource's progress, so a failure is counted instead, and the run raises once the
# same resource keeps failing.
_FAILED_RUNS_BEFORE_RAISE = 3
# A DNS label: the subdomain becomes part of the only host requests go to.
_SUBDOMAIN_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")
# Help Center locales look like "en-us" or "pt-br"; the value goes into the URL path.
_LOCALE_RE = re.compile(r"^[a-z]{2,3}(?:-[a-z0-9]{2,8})*$")
# dlt snake-cases table names, so "Acme-EU" and "acme_eu" would share a table while
# keeping separate sync states. Only names dlt leaves unchanged are accepted.
_RESOURCE_NAME_RE = re.compile(r"^[a-z][a-z0-9_]{0,50}$")
_COLUMNS = {
    "_deleted": {"data_type": "bool", "hard_delete": True},
    "title": {"data_type": "text", "nullable": True},
    "content": {"data_type": "text", "nullable": True},
    "url": {"data_type": "text", "nullable": True},
}


class ZendeskAPIError(RuntimeError):
    """A Zendesk API call failed.

    ``status`` is the HTTP status when Zendesk answered (``None`` otherwise), and
    ``transport`` is set when it could not be reached at all. ``ends_run`` marks
    errors that would fail the other resource the same way.
    """

    def __init__(
        self,
        message: str,
        status: int | None = None,
        transport: bool = False,
        ends_run: bool | None = None,
    ):
        super().__init__(message)
        self.status = status
        self.transport = transport
        self._ends_run = ends_run

    @property
    def ends_run(self) -> bool:
        if self._ends_run is not None:
            return self._ends_run
        return self.transport


class ZendeskAuthError(ZendeskAPIError):
    """Zendesk rejected the credentials (401), or the token lacks the ticket export (403)."""

    @property
    def ends_run(self) -> bool:
        return True


class ZendeskRateLimitError(ZendeskAPIError):
    """Still rate limited after the retries, or asked to wait longer than we sleep."""

    def __init__(self, message: str, retry_after: float | None = None):
        super().__init__(message, 429)
        self.retry_after = retry_after


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    # urllib keeps the Authorization header on a redirect to another host, so a
    # redirect is never followed: it surfaces as the 3xx HTTPError instead.
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _endpoint(url: str) -> str:
    """The request path with record ids replaced, for errors and logs."""
    return re.sub(r"/\d+", "/{id}", urllib.parse.urlparse(url).path)


# ---------------------------------------------------------------------------
# HTTP client (stdlib only)
# ---------------------------------------------------------------------------
class ZendeskClient:
    """Minimal Zendesk REST client: ``get(path_or_url, **params) -> dict``.

    Retries 429 (honouring ``Retry-After`` up to 30 s), 5xx and dropped connections,
    and raises :class:`ZendeskAPIError` otherwise. Absolute pagination links are only
    followed on ``https://<subdomain>.zendesk.com``.
    """

    def __init__(
        self,
        subdomain: str,
        *,
        oauth_token: str | None = None,
        email: str | None = None,
        api_token: str | None = None,
        timeout: float = 60.0,
        sleep: Callable[[float], None] = time.sleep,
        check_active: Callable[[], None] | None = None,
    ):
        if not isinstance(subdomain, str) or not _SUBDOMAIN_RE.match(subdomain):
            raise ValueError("Pass subdomain=, the 'acme' of acme.zendesk.com.")
        oauth_token, email, api_token = (_credential(v) for v in (oauth_token, email, api_token))
        self._host = f"{subdomain}.zendesk.com"
        self.base = f"https://{self._host}"
        if oauth_token:
            self._auth = f"Bearer {oauth_token}"
        elif email and api_token:
            raw = f"{email}/token:{api_token}".encode()
            self._auth = "Basic " + base64.b64encode(raw).decode()
        else:
            raise ValueError(
                "Zendesk credentials required: pass oauth_token=, or email= and api_token=."
            )
        self._timeout = timeout
        self._sleep = sleep
        self._check_active = check_active
        self._opener = urllib.request.build_opener(_NoRedirect)

    def __repr__(self) -> str:
        # The credentials are a password: keep them out of any repr, log or traceback.
        return f"ZendeskClient(host={self._host!r}, auth=<redacted>)"

    def _url(self, path_or_url: str, params: dict) -> str:
        if path_or_url.startswith("/"):
            url = self.base + path_or_url
        else:
            parsed = urllib.parse.urlparse(path_or_url)
            if parsed.scheme != "https" or parsed.netloc != self._host:
                raise ZendeskAPIError("Refusing to follow a pagination link to another host.")
            url = path_or_url
        if params:
            sep = "&" if "?" in url else "?"
            url += sep + urllib.parse.urlencode({k: v for k, v in params.items() if v is not None})
        return url

    def _open(self, request):
        return self._opener.open(request, timeout=self._timeout)

    def _wait(self, seconds: float) -> None:
        # A host that revoked access must not wait through the retries.
        if self._check_active is not None:
            self._check_active()
        self._sleep(seconds)

    def get(self, path_or_url: str, **params: Any) -> dict:
        url = self._url(path_or_url, params)
        endpoint = _endpoint(url)
        for attempt in range(_MAX_RETRIES):
            request = urllib.request.Request(
                url, headers={"Authorization": self._auth, "Accept": "application/json"}
            )
            try:
                with self._open(request) as response:
                    body = response.read()
            except urllib.error.HTTPError as exc:
                # Every raise below uses ``from None``: the HTTPError carries the URL and
                # headers of the request, and errors name the endpoint without record ids.
                status = exc.code
                if status == 401:
                    raise ZendeskAuthError(
                        "Zendesk rejected the credentials (HTTP 401). OAuth access tokens "
                        "expire; mint a fresh one.",
                        401,
                    ) from None
                if 300 <= status < 400:
                    raise ZendeskAPIError(
                        f"Zendesk redirected {endpoint} (HTTP {status}); it is not followed, "
                        "so credentials only ever go to the account host.",
                        status,
                    ) from None
                if status == 429:
                    delay = _retry_after(exc.headers)
                    if delay is not None and delay > _MAX_RETRY_AFTER:
                        raise ZendeskRateLimitError(
                            f"Zendesk asked to wait {delay:.0f}s on {endpoint}.", delay
                        ) from None
                    if attempt == _MAX_RETRIES - 1:
                        raise ZendeskRateLimitError(
                            f"Zendesk still rate limited {endpoint} after {_MAX_RETRIES} tries.",
                            delay,
                        ) from None
                if (status == 429 or status >= 500) and attempt < _MAX_RETRIES - 1:
                    delay = _retry_after(exc.headers)
                    delay = delay if delay is not None else _backoff(attempt)
                    logger.warning(
                        "Zendesk: HTTP %s on %s, retrying in %.0fs (%d/%d).",
                        status,
                        endpoint,
                        delay,
                        attempt + 1,
                        _MAX_RETRIES,
                    )
                    self._wait(delay)
                    continue
                raise ZendeskAPIError(
                    f"Zendesk returned HTTP {status} for {endpoint}.", status
                ) from None
            except (OSError, http.client.HTTPException) as exc:
                # URLError, timeouts, ConnectionResetError, RemoteDisconnected and
                # IncompleteRead: a dropped connection is retried like a 5xx.
                if attempt < _MAX_RETRIES - 1:
                    self._wait(_backoff(attempt))
                    continue
                # ``from None``: the error's text can carry the request URL.
                raise ZendeskAPIError(
                    f"Zendesk could not be reached ({type(exc).__name__}) for {endpoint}.",
                    transport=True,
                ) from None
            # Every endpoint used answers with a JSON object. An empty or garbled 200
            # must not read as "nothing there"; raise like any other failed request.
            try:
                payload = json.loads(body)
            except ValueError:
                payload = None
            if not isinstance(payload, dict):
                # A login or maintenance page answered with 200: it would read the same
                # for every request, so the run stops instead of setting records aside.
                raise ZendeskAPIError(
                    f"Zendesk returned HTTP 200 without a JSON object for {endpoint}.",
                    200,
                    ends_run=True,
                )
            return payload


def _credential(value: str | None) -> str | None:
    """A credential without surrounding whitespace, refused if it holds any inside.

    A pasted token often ends in a newline, and urllib quotes the whole header,
    token included, in the error it raises for one. The message never echoes it.
    """
    if value is None:
        return None
    value = value.strip()
    if any(ch.isspace() or ord(ch) < 32 for ch in value):
        raise ValueError("A Zendesk credential contains whitespace or control characters.")
    return value or None


def _retry_after(headers) -> float | None:
    try:
        seconds = float((headers or {}).get("Retry-After"))
    except (TypeError, ValueError):
        return None
    return seconds if 0 <= seconds < float("inf") else None


def _backoff(attempt: int) -> float:
    return float(min(_MAX_RETRY_AFTER, 2 ** (attempt + 1)))


class _CheckedClient:
    """Runs the host's ``check_active`` before every API request.

    All requests of a run happen before its first rows are emitted, so checking
    only around rows would let a sync whose access was revoked keep calling
    Zendesk until the end.
    """

    def __init__(self, client, check_active: Callable[[], None]):
        self._client = client
        self._check_active = check_active

    def __repr__(self) -> str:
        return f"_CheckedClient({self._client!r})"

    def get(self, path_or_url: str, **params: Any) -> dict:
        self._check_active()
        return self._client.get(path_or_url, **params)


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------
class _TextExtractor(HTMLParser):
    _BREAKS = frozenset({"p", "div", "br", "li", "h1", "h2", "h3", "h4", "h5", "h6", "tr", "pre"})

    def __init__(self):
        super().__init__()
        self.parts: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag == "li":
            self.parts.append("\n- ")
        elif tag in self._BREAKS:
            self.parts.append("\n")
        if tag == "img":
            alt = dict(attrs).get("alt")
            if alt:
                self.parts.append(f"[image: {alt}]")

    def handle_endtag(self, tag):
        if tag in self._BREAKS:
            self.parts.append("\n")

    def handle_data(self, data):
        self.parts.append(data)


def html_to_text(html: str | None) -> str:
    """Article bodies are HTML; keep the text and paragraph breaks."""
    if not html:
        return ""
    parser = _TextExtractor()
    parser.feed(html)
    lines = [" ".join(line.split()) for line in "".join(parser.parts).splitlines()]
    out, blank = [], False
    for line in lines:
        if line:
            out.append(line)
            blank = False
        elif not blank and out:
            out.append("")
            blank = True
    return "\n".join(out).strip()


def _user_label(users: dict, user_id, include_emails: bool = False) -> str:
    user = users.get(user_id) or {}
    name, email = user.get("name"), user.get("email") if include_emails else None
    if name and email:
        return f"{name} <{email}>"
    return name or email or (f"user {user_id}" if user_id else "unknown")


def ticket_to_row(
    ticket: dict,
    comments: list[dict],
    users: dict,
    subdomain: str,
    include_internal: bool,
    include_emails: bool = False,
) -> dict[str, Any]:
    """Render a ticket and its comment thread as one deterministic document row.

    ``updated_at`` is left out on purpose: Zendesk bumps it for metadata-only
    changes (SLA timers, triggers), which would otherwise re-cognify an
    unchanged conversation. People are named without their email address unless
    ``include_emails`` is on.
    """
    header = [
        f"Ticket #{ticket['id']}: {ticket.get('subject') or '(no subject)'}",
        f"Status: {ticket.get('status')}",
    ]
    for label, key in (("Priority", "priority"), ("Type", "type")):
        if ticket.get(key):
            header.append(f"{label}: {ticket[key]}")
    if ticket.get("tags"):
        header.append("Tags: " + ", ".join(sorted(ticket["tags"])))
    header.append(f"Requester: {_user_label(users, ticket.get('requester_id'), include_emails)}")
    if ticket.get("assignee_id"):
        header.append(f"Assignee: {_user_label(users, ticket['assignee_id'], include_emails)}")
    header.append(f"Created: {ticket.get('created_at')}")

    thread = []
    for comment in comments:
        public = comment.get("public", True)
        if not public and not include_internal:
            continue
        body = (comment.get("plain_body") or comment.get("body") or "").strip()
        if not body:
            continue
        who = _user_label(users, comment.get("author_id"), include_emails)
        kind = "" if public else " [internal note]"
        thread.append(f"--- {who}, {comment.get('created_at')}{kind}\n{body}")

    content = "\n".join(header) + ("\n\n" + "\n\n".join(thread) if thread else "")
    return {
        "id": str(ticket["id"]),
        "title": f"Ticket #{ticket['id']}: {ticket.get('subject') or '(no subject)'}",
        "content": content,
        "url": f"https://{subdomain}.zendesk.com/agent/tickets/{ticket['id']}",
        "_deleted": False,
    }


def _article_key(article: dict) -> str:
    # The listing can hold one object per translation, all with the same id, so the
    # locale is part of the key (and the row id): two translations never collapse.
    return f"{article['id']}:{article.get('locale') or ''}"


def article_to_row(article: dict) -> dict[str, Any]:
    body = html_to_text(article.get("body"))
    labels = article.get("label_names") or []
    content = body + (f"\n\nLabels: {', '.join(sorted(labels))}" if labels else "")
    return {
        "id": _article_key(article),
        "title": article.get("title") or f"Article {article['id']}",
        "content": content,
        "url": article.get("html_url"),
        "_deleted": False,
    }


def deleted_row(row_id: str) -> dict[str, Any]:
    return {"id": str(row_id), "_deleted": True}


# ---------------------------------------------------------------------------
# Sync
# ---------------------------------------------------------------------------
def _check_owner(state: dict, subdomain: str) -> None:
    """Refuse a sync state written for another Zendesk account."""
    if state.get("subdomain") not in (None, subdomain):
        raise ValueError(
            f"This sync state belongs to Zendesk '{state['subdomain']}', not '{subdomain}'. "
            "Give each account its own resource_name."
        )


def _list_articles(client, path: str) -> dict[str, dict]:
    """Every article, keyed by id and locale, checked against Zendesk's ``count``.

    A complete listing is the only delete signal for articles. The listing uses
    cursor pagination (offset pagination stops at 10,000 records with a 400), which
    reports no total, so the ``count`` comes from one offset request first. A listing
    that does not add up to it raises instead of letting the articles it missed be
    forgotten; one that shifts while it is read fails the same check and is read
    again on the next run.
    """
    expected = client.get(path, per_page=1).get("count")
    if not isinstance(expected, int):
        raise ZendeskAPIError("Zendesk article listing has no count; nothing is forgotten.")
    articles: dict[str, dict] = {}
    data = client.get(path, **{"page[size]": _PAGE_SIZE})
    seen_links: set[str] = set()
    while True:
        for article in data.get("articles") or []:
            if isinstance(article, dict) and article.get("id") is not None:
                articles[_article_key(article)] = article
        if not (data.get("meta") or {}).get("has_more"):
            break
        nxt = (data.get("links") or {}).get("next")
        if not nxt or nxt in seen_links:
            raise ZendeskAPIError("Zendesk article pagination did not move forward; stopping.")
        seen_links.add(nxt)
        data = client.get(nxt)
    if len(articles) != expected:
        raise ZendeskAPIError(
            f"Zendesk listed {len(articles)} of {expected} articles; nothing is forgotten."
        )
    return articles


def _ticket_comments(client, ticket_id) -> tuple[list[dict], dict]:
    comments, users = [], {}
    data = client.get(f"/api/v2/tickets/{ticket_id}/comments.json", include="users")
    seen_links: set[str] = set()
    while True:
        comments.extend(data.get("comments") or [])
        for user in data.get("users") or []:
            if isinstance(user, dict) and user.get("id") is not None:
                users[user["id"]] = user
        nxt = data.get("next_page")
        if not nxt:
            return comments, users
        if nxt in seen_links:
            raise ZendeskAPIError("Zendesk comment pagination did not move forward; stopping.")
        seen_links.add(nxt)
        data = client.get(nxt)


def _users_by_id(client, ids) -> dict:
    """Names for the given user ids, 100 per request (show_many's limit).

    The lookup only supplies display names, so when it fails the tickets still sync
    and people show up as ``user <id>``.
    """
    ids = sorted(ids)
    found: dict = {}
    try:
        for i in range(0, len(ids), 100):
            chunk = ",".join(str(x) for x in ids[i : i + 100])
            for user in client.get("/api/v2/users/show_many.json", ids=chunk).get("users") or []:
                if isinstance(user, dict) and user.get("id") is not None:
                    found[user["id"]] = user
    except ZendeskAPIError as exc:
        if exc.ends_run or isinstance(exc, ZendeskRateLimitError):
            raise
        logger.warning("Zendesk: user lookup failed (%s); names fall back to user ids.", exc)
    return found


def _read_export(client, state: dict, max_pages: int | None) -> tuple[dict, str | None]:
    """The changed tickets since the saved cursor (newest version of each), and the
    cursor to save once they are out."""
    cursor = state.get("after_cursor")
    params = {"cursor": cursor} if cursor else {"start_time": 0}
    pages = 0
    # The export lists a ticket once per change, so it can appear several times in
    # one window (edited, then deleted, then restored). Keep only its newest
    # version, so the last change wins and comments are fetched once per ticket.
    latest: dict = {}
    while True:
        try:
            data = client.get("/api/v2/incremental/tickets/cursor.json", **params)
        except ZendeskAPIError as exc:
            if exc.status == 403:
                raise ZendeskAuthError(
                    "Zendesk refused the ticket export (HTTP 403): it needs an admin's "
                    "credentials.",
                    403,
                ) from None
            raise
        for ticket in data.get("tickets") or []:
            if not isinstance(ticket, dict) or ticket.get("id") is None:
                continue
            latest.pop(ticket["id"], None)
            latest[ticket["id"]] = ticket
        pages += 1
        next_cursor = data.get("after_cursor")
        if data.get("end_of_stream"):
            return latest, next_cursor or cursor
        if not next_cursor:
            raise ZendeskAPIError("Zendesk export page has no cursor and no end; stopping.")
        if next_cursor == cursor:
            raise ZendeskAPIError("Zendesk export cursor did not move forward; stopping.")
        cursor = next_cursor
        if max_pages is not None and pages >= max_pages:
            behind = _hours_behind(data.get("end_time"))
            logger.warning(
                "Zendesk: stopped after %d export page(s) with more ticket changes waiting%s; "
                "the next run continues. If this repeats, raise max_export_pages_per_run "
                "or sync more often.",
                pages,
                f" ({behind:.1f} hours behind)" if behind is not None else "",
            )
            return latest, cursor
        params = {"cursor": cursor}


def _hours_behind(end_time) -> float | None:
    try:
        return max(0.0, (time.time() - float(end_time)) / 3600)
    except (TypeError, ValueError):
        return None


def _set_aside(pending: dict[str, int], key: str, stats: dict) -> None:
    """Count another failed run for a ticket, giving it up after ``_GIVE_UP_RUNS``."""
    runs = pending.get(key, 0) + 1
    if runs >= _GIVE_UP_RUNS:
        pending.pop(key, None)
        stats["given_up"] = stats.get("given_up", 0) + 1
        logger.warning(
            "Zendesk: a ticket's comments failed %d runs in a row; it stays as stored.", runs
        )
    else:
        pending[key] = runs
        stats["kept"] = stats.get("kept", 0) + 1


def sync_tickets(
    client,
    state: dict,
    subdomain: str,
    include_internal: bool = False,
    max_pages: int | None = DEFAULT_EXPORT_PAGES_PER_RUN,
    stats: dict | None = None,
    include_emails: bool = False,
) -> Iterator[dict[str, Any]]:
    """Yield changed tickets (with comments) and tombstones for deleted tickets.

    The first run starts the export at ``start_time=0``; later runs resume from
    ``state['after_cursor']``. Every request happens before the first row and the
    state is written after the last one, and dlt persists it only after the load
    succeeds, so a failed run reads the same window again.

    When ``include_internal`` changes, the export starts over and that run reads it
    to the end, so every stored ticket is rendered again at once: turning internal
    notes off removes them from memory on the next sync.
    """
    if stats is None:
        stats = {}
    _check_owner(state, subdomain)
    flipped = state.get("include_internal", include_internal) != include_internal
    read_from = state
    if flipped:
        logger.info("Zendesk: include_internal_notes changed; re-reading every ticket.")
        read_from = {**state, "after_cursor": None}
    latest, cursor = _read_export(client, read_from, None if flipped else max_pages)

    # Tickets set aside by earlier runs because their comments could not be read.
    pending: dict[str, int] = dict(state.get("pending", {}))
    for key in sorted(set(pending) - {str(t) for t in latest}, key=int):
        try:
            found = client.get(f"/api/v2/tickets/{key}.json").get("ticket")
        except ZendeskAPIError as exc:
            if exc.ends_run or isinstance(exc, ZendeskRateLimitError):
                raise
            if exc.status in (404, 410):
                latest[int(key)] = {"id": int(key), "status": "deleted"}
            else:
                _set_aside(pending, key, stats)
            continue
        if isinstance(found, dict) and found.get("id") is not None:
            latest[int(key)] = found
        else:
            # An answer without the ticket is not a deletion: keep it set aside.
            _set_aside(pending, key, stats)

    rows: list[dict] = []
    live: list[tuple[dict, list]] = []
    users: dict = {}
    streak = 0
    queue = list(latest.items())
    for position, (tid, ticket) in enumerate(queue):
        if ticket.get("status") == "deleted":
            pending.pop(str(tid), None)
            rows.append(deleted_row(tid))
            continue
        try:
            comments, authors = _ticket_comments(client, tid)
        except ZendeskAPIError as exc:
            if exc.ends_run or isinstance(exc, ZendeskRateLimitError):
                raise
            if exc.status in (404, 410):
                # The ticket was deleted after the export listed it.
                pending.pop(str(tid), None)
                rows.append(deleted_row(tid))
                continue
            # Only fresh failures count: a ticket already set aside fails the same way
            # every run, and counting it would stop every run before it is given up.
            if str(tid) not in pending:
                streak = streak + 1 if (exc.status or 0) >= 500 else 0
            _set_aside(pending, str(tid), stats)
            if streak >= _OUTAGE_STREAK:
                # Zendesk itself is likely down. Stop asking, but cleanly: what was read
                # is kept, the rest of the page is set aside for the next run (which asks
                # for these as retries, so they no longer count), and the cursor moves.
                for later_tid, later in queue[position + 1 :]:
                    if later.get("status") == "deleted":
                        pending.pop(str(later_tid), None)
                        rows.append(deleted_row(later_tid))
                    else:
                        pending.setdefault(str(later_tid), 0)
                stats["outage"] = 1
                logger.warning(
                    "Zendesk: %d ticket comment requests failed in a row; stopping this run, "
                    "the next one asks again.",
                    streak,
                )
                break
            continue
        streak = 0
        pending.pop(str(tid), None)
        users.update(authors)
        live.append((ticket, comments))
    # include=users only brings the people who wrote a comment, so an assignee who
    # never replied would show up as "user <id>". Look the rest up in one go.
    missing = {
        t.get(key) for t, _ in live for key in ("requester_id", "assignee_id") if t.get(key)
    } - set(users)
    if missing:
        users.update(_users_by_id(client, missing))
    rows.extend(
        ticket_to_row(ticket, comments, users, subdomain, include_internal, include_emails)
        for ticket, comments in live
    )
    deleted = sum(1 for row in rows if row["_deleted"])
    logger.info(
        "Zendesk: %d changed ticket(s), %d deleted, %d set aside.",
        len(rows) - deleted,
        deleted,
        len(pending),
    )
    yield from rows

    # Only reached once every row is out: a run stopped mid-way saves nothing.
    state["subdomain"] = subdomain
    state["include_internal"] = include_internal
    state["after_cursor"] = cursor
    state["pending"] = pending


def sync_articles(
    client, state: dict, subdomain: str, locale: str | None = None
) -> Iterator[dict[str, Any]]:
    """Yield new/edited published articles and tombstones for removed ones.

    ``state['versions']`` maps ``<article id>:<locale>`` -> ``updated_at`` of the
    version stored. The listing must complete before anything is tombstoned: an API
    error raises, so a failed run can never forget live articles.
    """
    _check_owner(state, subdomain)
    path = (
        f"/api/v2/help_center/{locale}/articles.json"
        if locale
        else "/api/v2/help_center/articles.json"
    )
    listed = _list_articles(client, path)
    old: dict[str, str] = dict(state.get("versions", {}))
    versions: dict[str, str] = {}
    rows: list[dict] = []
    for key in sorted(listed):
        article = listed[key]
        if article.get("draft"):
            continue
        versions[key] = article.get("updated_at")
        if old.get(key) != versions[key]:
            rows.append(article_to_row(article))
    changed = len(rows)
    rows.extend(deleted_row(key) for key in sorted(set(old) - set(versions)))
    logger.info("Zendesk: %d new/edited article(s), %d removed.", changed, len(rows) - changed)
    yield from rows

    state["subdomain"] = subdomain
    state["versions"] = versions


def _isolated(name: str, rows: Callable[[dict], Iterator[dict]], state: dict, run: dict):
    """Run one resource so that its ordinary API errors do not fail the other one.

    Tickets and articles share no state. When one of them gets an error from
    Zendesk, its state is left as it was, the failure is counted, and the other one
    still finishes and saves its progress; the failed one is retried on the next
    run. Raising there would fail the whole pipeline run, and dlt would keep neither
    resource's progress, so the run only raises once the same resource has failed
    ``_FAILED_RUNS_BEFORE_RAISE`` runs in a row, or when every selected resource
    failed. Bad credentials and a dead connection (``ends_run``) raise at once; a
    rate limit stops the rest of the run cleanly, and the next run resumes.
    """
    counts = run["counts"].setdefault(name, {"scanned": 0, "deleted": 0})
    if run["stopped"]:
        return
    before = copy.deepcopy(dict(state))
    try:
        for row in rows(state):
            # dlt moves on to the other resource as soon as this one yields, so an
            # outage found while reading must stop the rest of the run before then.
            _note_outage(run)
            counts["deleted" if row.get("_deleted") else "scanned"] += 1
            yield row
        _note_outage(run)
        state.pop("failed_runs", None)
    except ZendeskRateLimitError:
        _restore(state, before)
        run["stopped"] = "rate_limit"
        logger.warning("Zendesk: rate limited; stopping this run, the next sync resumes.")
    except ZendeskAPIError as exc:
        _restore(state, before)
        run["failed"].add(name)
        if isinstance(exc, ZendeskAuthError):
            run["auth_failed"] = True
        if exc.ends_run or run["failed"] >= run["selected"]:
            raise
        failed_runs = before.get("failed_runs", 0) + 1
        if failed_runs >= _FAILED_RUNS_BEFORE_RAISE:
            raise ZendeskAPIError(
                f"Zendesk {name} failed {failed_runs} runs in a row: {exc}", exc.status
            ) from exc
        state["failed_runs"] = failed_runs
        logger.warning(
            "Zendesk: %s failed (%s); the other resource still syncs, %s are retried on the "
            "next run (%d of %d failed runs before this raises).",
            name,
            exc,
            name,
            failed_runs,
            _FAILED_RUNS_BEFORE_RAISE,
        )
    finally:
        _publish_stats(run)


def _begin(run: dict, name: str) -> None:
    """Start fresh counters when a reused source object runs again.

    dlt starts the resources in order once per run, so a resource that already
    started on this object means a new run: a rate limit or failure from the last
    run must not stop this one.
    """
    if name in run["started"]:
        run.update(started=set(), failed=set(), counts={}, extra={}, stopped=None)
        run["auth_failed"] = False
    run["started"].add(name)


def _note_outage(run: dict) -> None:
    if run["extra"].pop("outage", None):
        run["stopped"] = "outage"


def _restore(state: dict, before: dict) -> None:
    state.clear()
    state.update(before)


def _publish_stats(run: dict) -> None:
    """Count-only diagnostics for hosts (``cognee_sync_stats``), never ids or text."""
    stats = run["stats"]
    extra = run["extra"]
    stats.clear()
    stats["scanned"] = sum(c["scanned"] for c in run["counts"].values())
    stats["deleted"] = sum(c["deleted"] for c in run["counts"].values())
    stats["kept"] = extra.get("kept", 0)
    stats["given_up"] = extra.get("given_up", 0)
    stats["failed"] = len(run["failed"]) + (1 if run["stopped"] else 0)
    for name in sorted(run["failed"]):
        stats[f"failed_{name}"] = 1
    if run["auth_failed"]:
        stats["failed_auth"] = 1
    if run["stopped"]:
        stats[f"failed_{run['stopped']}"] = 1


# ---------------------------------------------------------------------------
# Public factory
# ---------------------------------------------------------------------------
def zendesk_source(
    subdomain: str | None = None,
    *,
    oauth_token: str | None = None,
    email: str | None = None,
    api_token: str | None = None,
    resources: tuple[str, ...] | list[str] = _RESOURCES,
    include_internal_notes: bool = False,
    include_emails: bool = False,
    locale: str | None = None,
    max_export_pages_per_run: int | None = DEFAULT_EXPORT_PAGES_PER_RUN,
    resource_name: str | None = None,
    check_active: Callable[[], None] | None = None,
    client: Any = None,
    service: Any = None,
):
    """Create a dlt source with ``<resource_name>_tickets`` and/or
    ``<resource_name>_articles``.

    Args:
        subdomain: The ``<subdomain>`` of ``<subdomain>.zendesk.com``.
        oauth_token: OAuth access token (sent as ``Bearer``).
        email / api_token: Legacy API-token auth, used when no OAuth token is given.
        resources: Any of ``"tickets"`` and ``"articles"``.
        include_internal_notes: Also ingest private agent notes (off by default;
            they often hold internal or sensitive information).
        include_emails: Name people as ``Name <email>`` (off by default: names only).
        locale: Help Center locale to list articles for (all the account lists when
            None).
        max_export_pages_per_run: Ticket export pages read per run (each holds up
            to 1000 tickets); the next run continues from the saved cursor, so a
            large first backfill makes progress on every run. ``None`` reads the
            whole export in one run.
        resource_name: Prefix of the two table names, and the key of the sync state:
            lowercase letters, digits and underscores. Defaults to
            ``zendesk_<subdomain>``, so two Zendesk accounts in one dataset never share
            a table (their ids overlap). Keep it fixed across runs.
        check_active: Optional host authorization checkpoint, called before every
            API request, before every retry wait and around every row (``dlt_utils.guarded_rows``);
            raising from it stops the sync.
        client: Object with ``get(path_or_url, **params)``; ``service`` is an alias.

    Returns:
        A dlt source. Every resource uses ``primary_key="id"``,
        ``write_disposition="merge"`` and an ``_deleted`` hard-delete column.
        After a run, ``source.cognee_sync_stats`` holds counts only: ``scanned``,
        ``deleted``, ``kept`` (tickets set aside for another try), ``given_up``,
        ``failed``, plus ``failed_tickets``, ``failed_articles``, ``failed_auth`` or
        ``failed_rate_limit`` when a part failed for that.
    """
    import dlt

    if getattr(dlt_utils, "DOCUMENT_SYNC_VERSION", 0) < 2:
        raise RuntimeError(
            "The Zendesk connector requires cognee 1.6.3 or later (table-scoped document "
            "cleanup). Upgrade cognee before syncing."
        )
    if client is not None and service is not None and client is not service:
        raise ValueError("Pass client= or service=, not both.")
    client = client if client is not None else service
    unknown = set(resources) - set(_RESOURCES)
    if unknown:
        raise ValueError(f"Unknown Zendesk resources {sorted(unknown)}; choose from {_RESOURCES}.")
    subdomain = (subdomain or "").strip().lower()
    if not _SUBDOMAIN_RE.match(subdomain):
        raise ValueError("Pass subdomain=, the 'acme' of acme.zendesk.com.")
    if locale is not None and not _LOCALE_RE.match(locale.lower()):
        raise ValueError(f"'{locale}' is not a Help Center locale like 'en-us'.")
    locale = locale.lower() if locale else None
    if max_export_pages_per_run is not None and (
        not isinstance(max_export_pages_per_run, int) or max_export_pages_per_run < 1
    ):
        raise ValueError("max_export_pages_per_run must be a positive number, or None.")
    if resource_name and not _RESOURCE_NAME_RE.match(resource_name):
        raise ValueError(
            "resource_name must be lowercase letters, digits and underscores, starting with a "
            "letter, so dlt uses it unchanged as the table name."
        )
    resource_name = resource_name or f"zendesk_{subdomain.replace('-', '_')}"
    if client is None:
        client = ZendeskClient(
            subdomain,
            oauth_token=oauth_token,
            email=email,
            api_token=api_token,
            check_active=check_active,
        )
    if check_active is not None:
        client = _CheckedClient(client, check_active)
    run: dict = {
        "selected": {name for name in _RESOURCES if name in resources},
        "failed": set(),
        "counts": {},
        "extra": {},
        "stopped": None,
        "auth_failed": False,
        "stats": {},
        "started": set(),
    }

    @dlt.resource(
        name=f"{resource_name}_tickets",
        primary_key="id",
        write_disposition="merge",
        columns=_COLUMNS,
    )
    def zendesk_tickets():
        def rows(state):
            return sync_tickets(
                client,
                state,
                subdomain,
                include_internal=include_internal_notes,
                max_pages=max_export_pages_per_run,
                stats=run["extra"],
                include_emails=include_emails,
            )

        _begin(run, "tickets")
        state = dlt.current.resource_state()
        yield from guarded_rows(_isolated("tickets", rows, state, run), check_active)

    @dlt.resource(
        name=f"{resource_name}_articles",
        primary_key="id",
        write_disposition="merge",
        columns=_COLUMNS,
    )
    def zendesk_articles():
        def rows(state):
            return sync_articles(client, state, subdomain, locale=locale)

        _begin(run, "articles")
        state = dlt.current.resource_state()
        yield from guarded_rows(_isolated("articles", rows, state, run), check_active)

    selected = {"tickets": zendesk_tickets, "articles": zendesk_articles}

    @dlt.source(name=ZENDESK_SOURCE_NAME)
    def _zendesk():
        return [selected[name] for name in _RESOURCES if name in resources]

    source = _zendesk()
    # Tickets and articles are prose: route them through cognify (document mode).
    setattr(source, DOCUMENT_SOURCE_ATTR, ZENDESK_SOURCE_NAME)
    # Keep the export cursor per resource_name (and, in cognee, per dataset), so
    # syncing two accounts or datasets never resumes from the other's cursor.
    setattr(source, PIPELINE_SCOPE_ATTR, resource_name)
    _publish_stats(run)
    source.cognee_sync_stats = run["stats"]
    return source
