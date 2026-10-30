"""Intercom connector for cognee: a ``dlt`` source that turns support content into memory.

One document per conversation (with its replies and internal notes), per Help
Center article and, opt-in, per contact note, synced incrementally with
forget-on-delete. Hand the source to :func:`cognee.remember` with
``write_disposition="merge"``::

    await cognee.remember(
        intercom_source(token=os.environ["INTERCOM_ACCESS_TOKEN"]),
        dataset_name="intercom",
        write_disposition="merge",
    )

The README describes what is stored, how changes and deletions are found, and what a
run does when Intercom fails part of the way through.
"""

from __future__ import annotations

import hashlib
import html
import json
import re
import time
from collections.abc import Callable, Iterator
from datetime import datetime, timezone
from typing import Any

import dlt
import requests
from cognee.shared.logging_utils import get_logger
from cognee.tasks.ingestion import dlt_utils
from cognee.tasks.ingestion.dlt_utils import DOCUMENT_SOURCE_ATTR
from dlt.common.normalizers.naming.snake_case import NamingConvention

logger = get_logger("intercom_connector")

INTERCOM_SOURCE_NAME = "intercom"
# Tables are "<resource_name>_conversations", "_articles" and "_contact_notes".
_TABLE_SUFFIXES = ("conversations", "articles", "contact_notes")

DEFAULT_BASE_URL = "https://api.intercom.io"
# Pin the API version so upstream changes can't silently alter parsing.
_INTERCOM_VERSION = "2.16"

# Intercom's maximum page size for cursor-paginated endpoints.
_PAGE_SIZE = 150
_ARTICLES_PAGE_SIZE = 50
# Re-query this far before the cursor: the search index can trail recent writes.
_SEARCH_OVERLAP_SECONDS = 300
# Attempts per request, and the longest single wait.
_MAX_ATTEMPTS = 3
_MAX_RETRY_DELAY_SECONDS = 60.0
# Retries sleep inside cognee's staging lock, which every other dlt ingestion in
# the process waits on, so a run waits at most this long in total.
_WAIT_BUDGET_SECONDS = 60.0
_MAX_PAGES = 100_000
_STAT_KEYS = (
    "scanned",
    "skipped",
    "changed",
    "deleted",
    "failed",
    "failed_auth",
    "failed_rate_limit",
    "requests",
)
_REQUEST_TIMEOUT_SECONDS = 30


_TOMBSTONE_COLUMNS = {"_deleted": {"data_type": "bool", "hard_delete": True}}
# C0/C1 control characters (tab and newline kept) and lone surrogates: a NUL makes
# Postgres reject the load and a lone surrogate fails the dlt extract.
_UNSAFE_CHARS = re.compile("[\x00-\x08\x0b-\x1f\x7f-\x9f\ud800-\udfff]")

_AUTHOR_ROLES = {
    "user": "Customer",
    "lead": "Customer",
    "contact": "Customer",
    "admin": "Agent",
    "bot": "Bot",
    "team": "Team",
}

_BLOCK_END_RE = re.compile(r"(?i)<br\s*/?>|</(p|div|li|h[1-6]|tr|blockquote|pre)>")
_TAG_RE = re.compile(r"<[^>]+>")
# Non-breaking spaces (from &nbsp;) are folded into ordinary spaces too.
_SPACES_RE = re.compile(r"[ \t\r\f\v\xa0]+")


# ---------------------------------------------------------------------------
# Public factory
# ---------------------------------------------------------------------------
def intercom_source(
    token: str | None = None,
    base_url: str = DEFAULT_BASE_URL,
    include_conversations: bool = True,
    include_articles: bool = True,
    include_draft_articles: bool = False,
    include_contact_notes: bool = False,
    service: Any = None,
    *,
    resource_name: str = INTERCOM_SOURCE_NAME,
    check_active: Callable[[], None] | None = None,
    session: Any = None,
):
    """Create a dlt source that yields Intercom content as documents.

    Args:
        token: Intercom access token. Required unless ``service`` is given.
        base_url: API host for the workspace's data region.
        include_conversations: Sync conversations with their replies and notes.
        include_articles: Sync Help Center articles.
        include_draft_articles: Also sync unpublished articles.
        include_contact_notes: Also sync notes attached to contacts.
        service: Pre-built ``requests``-compatible session (tests, proxies). It is
            used as is and not closed; when omitted, each run opens and closes its own
            from the token.
        resource_name: Prefix of the three dlt tables (``<name>_conversations``,
            ``<name>_articles``, ``<name>_contact_notes``), which also scopes the sync
            state. A dataset's state remembers its workspace, and a token for another
            workspace stops the run, so give each workspace its own name in a shared
            dataset.
        check_active: Optional host checkpoint, called before every request and before
            every row is published; raising from it stops the run.
        session: Alias of ``service``. Give at most one.

    Returns:
        A dlt source for ``cognee.remember(..., write_disposition="merge")``. After a
        run, ``source.cognee_sync_stats`` holds counts only, summed over the three
        content types: ``scanned``, ``changed``, ``deleted``, ``failed`` (items
        without an id), ``failed_auth`` and ``failed_rate_limit`` (1 when the run
        stopped for that reason), ``requests``, and ``complete`` (1 once all three
        finished).
    """
    if getattr(dlt_utils, "DOCUMENT_SYNC_VERSION", 0) < 1:
        raise RuntimeError(
            "The Intercom connector needs cognee's document sync cleanup (cognee>=1.6.3)."
        )
    base = str(base_url or "").strip().rstrip("/")
    if not base.startswith(("https://", "http://")):
        raise ValueError("base_url must be an Intercom API URL, e.g. https://api.intercom.io.")
    _check_resource_name(resource_name)
    if not (include_conversations or include_articles or include_contact_notes):
        raise ValueError("Nothing to sync: enable conversations, articles or contact notes.")
    if service is not None and session is not None:
        raise ValueError("Pass service= or session=, not both.")
    client = service if service is not None else session
    secret = _check_token(token)
    if client is None and not secret:
        raise ValueError("token is required (an Intercom access token).")
    stats: dict[str, int] = {}
    run = _RunStats(stats, _TABLE_SUFFIXES)

    def _run(name: str, sync: Callable[..., Iterator[dict]], enabled: bool) -> Iterator[dict]:
        part = run.start(name)
        state = dlt.current.resource_state()
        if not enabled:
            yield from forget_all(state, part, check_active)
            run.finish(name)
            return
        http = client if client is not None else _make_session(secret)
        try:
            api = IntercomAPI(http, base, check_active=check_active, stats=part)
            _check_workspace(api, state, resource_name)
            yield from sync(api, state, part, check_active)
        finally:
            if client is None:
                http.close()
            run.publish()  # a failed run still reports its requests and failure reason
        run.finish(name)

    @dlt.resource(
        name=f"{resource_name}_conversations",
        primary_key="id",
        write_disposition="merge",
        columns=_TOMBSTONE_COLUMNS,
    )
    def intercom_conversations():
        yield from _run("conversations", sync_conversations, include_conversations)

    @dlt.resource(
        name=f"{resource_name}_articles",
        primary_key="id",
        write_disposition="merge",
        columns=_TOMBSTONE_COLUMNS,
    )
    def intercom_articles():
        def sync(api, state, part, check):
            return sync_articles(api, include_draft_articles, state, part, check)

        yield from _run("articles", sync, include_articles)

    @dlt.resource(
        name=f"{resource_name}_contact_notes",
        primary_key="id",
        write_disposition="merge",
        columns=_TOMBSTONE_COLUMNS,
    )
    def intercom_contact_notes():
        yield from _run("contact_notes", sync_contact_notes, include_contact_notes)

    @dlt.source(name=INTERCOM_SOURCE_NAME)
    def _intercom():
        # All three resources always run: a disabled one only tombstones what it
        # synced before, so switching content off forgets it instead of leaving
        # stale documents in memory.
        return [intercom_conversations, intercom_articles, intercom_contact_notes]

    source = _intercom()
    # Opt into the document ingestion path (row -> text document -> cognify).
    # resolve_dlt_sources reads this marker; it never imports this connector.
    setattr(source, DOCUMENT_SOURCE_ATTR, INTERCOM_SOURCE_NAME)
    # The table prefix also scopes the dlt state (cursor, known ids, workspace), so
    # the state and the tables it describes always resolve to the same pipeline.
    setattr(source, dlt_utils.PIPELINE_SCOPE_ATTR, resource_name)
    source.cognee_sync_stats = stats
    return source


def _check_resource_name(name: Any) -> None:
    """A non-empty name that dlt keeps as it is, short enough for its table names.

    It prefixes the tables and scopes the state: two names dlt normalizes alike
    ("Intercom-EU", "intercom_eu") would share tables.
    """
    if not isinstance(name, str) or not name or len(name) > 48:
        raise ValueError("resource_name must be a non-empty string of at most 48 characters.")
    if NamingConvention().normalize_table_identifier(name) != name:
        raise ValueError("resource_name must be lowercase snake_case (letters, digits, _).")


def _check_workspace(api: IntercomAPI, state: dict, resource_name: str) -> None:
    """Stop before a token for another workspace can overwrite this state's documents.

    Conversation and article ids are only unique within a workspace, so a second
    workspace synced under the same ``resource_name`` would overwrite, and then
    tombstone, the first one's documents.
    """
    me = api.request("GET", "/me")
    workspace = str((me.get("app") or {}).get("id_code") or "")
    known = state.get("workspace")
    if workspace and known and known != workspace and state.get("known_ids"):
        raise IntercomSyncError(
            f"This dataset already holds Intercom workspace {known} under resource_name "
            f"{resource_name!r}, but the token belongs to workspace {workspace}. Pass a "
            "different resource_name= to sync another workspace into the same dataset."
        )
    if workspace:
        state["workspace"] = workspace


class IntercomSyncError(RuntimeError):
    """The run cannot be trusted (stuck paging, an emptied listing, a bad response)."""


class _RunStats:
    """Per-content-type counts, published as flat totals on the source after each step."""

    def __init__(self, stats: dict, names: tuple[str, ...]):
        self.stats = stats
        self.parts: dict[str, dict] = {name: {} for name in names}

    def start(self, name: str) -> dict:
        part = self.parts[name]
        part.clear()
        part.update(dict.fromkeys(_STAT_KEYS, 0), complete=0)
        self.publish()
        return part

    def finish(self, name: str) -> None:
        self.parts[name]["complete"] = 1
        self.publish()

    def publish(self) -> None:
        totals = {key: sum(part.get(key, 0) for part in self.parts.values()) for key in _STAT_KEYS}
        self.stats.clear()
        self.stats.update(totals)
        self.stats["complete"] = int(all(part.get("complete") for part in self.parts.values()))


def _check_token(token: str | None) -> str:
    """Strip a pasted token and reject a malformed one without echoing it."""
    secret = (token or "").strip()
    if secret and (not secret.isascii() or not secret.isprintable() or " " in secret):
        raise ValueError("The Intercom token contains spaces or control characters.")
    return secret


def _make_session(token: str) -> Any:
    session = requests.Session()
    session.headers.update(
        {
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
            "Intercom-Version": _INTERCOM_VERSION,
        }
    )
    return session


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------
class IntercomAPI:
    """Thin read-only wrapper: retries, cursor pagination and the region base URL."""

    def __init__(
        self,
        session: Any,
        base_url: str,
        check_active: Callable[[], None] | None = None,
        stats: dict | None = None,
    ):
        self.session = session
        self.base_url = base_url.rstrip("/")
        self.check_active = check_active
        self.stats = stats if stats is not None else {}
        self.waited = 0.0  # seconds slept on retries this run

    def request(
        self, method: str, path: str, params: dict | None = None, body: dict | None = None
    ) -> dict:
        """Call the API, retrying 429/5xx and network errors; other errors raise.

        A 429 waits for ``Retry-After`` or Intercom's ``X-RateLimit-Reset``; other
        retries back off exponentially. A permanent error (auth, not found), three
        failed attempts or a spent wait budget raises, which aborts the sync before
        any deletion.
        """
        url = f"{self.base_url}{path}"
        for attempt in range(_MAX_ATTEMPTS):
            if self.check_active is not None:
                self.check_active()
            self.stats["requests"] = self.stats.get("requests", 0) + 1
            failure: Exception | None = None
            try:
                response = self.session.request(
                    method, url, params=params, json=body, timeout=_REQUEST_TIMEOUT_SECONDS
                )
            except (requests.ConnectionError, requests.Timeout) as exc:
                failure, reason = exc, type(exc).__name__
                delay = min(2.0**attempt, _MAX_RETRY_DELAY_SECONDS)
            else:
                status = response.status_code
                if status != 429 and status < 500:
                    if status in (401, 403):
                        self.stats["failed_auth"] = 1
                    return _parse(response, path)
                reason, delay = f"HTTP {status}", _retry_delay(response.headers, attempt)
            if attempt == _MAX_ATTEMPTS - 1 or self.waited + delay > _WAIT_BUDGET_SECONDS:
                break
            logger.warning("Intercom: %s on %s; retrying in %.1fs.", reason, path, delay)
            self.waited += delay
            time.sleep(delay)
        if failure is not None:
            raise failure
        if response.status_code == 429:
            self.stats["failed_rate_limit"] = 1
            raise IntercomSyncError(
                f"Intercom is rate-limiting {path} (HTTP 429). The run stopped before "
                "anything was forgotten; the next run picks up from the last cursor."
            )
        raise IntercomSyncError(
            f"Intercom failed {path} with HTTP {response.status_code}. The run stopped "
            "before anything was forgotten; the next run picks up from the last cursor."
        )

    def paginate(
        self, method: str, path: str, key: str, body: dict | None = None
    ) -> Iterator[dict]:
        """Yield the items under ``key`` across every page of an Intercom list.

        Intercom uses two pagination styles: cursor pages, where ``pages.next`` is
        an object with ``starting_after`` (conversations, contacts, search), and
        numbered pages, where ``pages.next`` is a URL (a contact's notes).
        """
        starting_after = None
        page_number = 1
        for _ in range(_MAX_PAGES):
            if method == "POST":
                pagination: dict[str, Any] = {"per_page": _PAGE_SIZE}
                if starting_after:
                    pagination["starting_after"] = starting_after
                page = self.request("POST", path, body={**(body or {}), "pagination": pagination})
            else:
                params: dict[str, Any] = {"per_page": _PAGE_SIZE}
                if starting_after:
                    params["starting_after"] = starting_after
                if page_number > 1:
                    params["page"] = page_number
                page = self.request("GET", path, params=params)
            items = page.get(key) or []
            yield from items
            pages = page.get("pages") or {}
            next_page = pages.get("next")
            if isinstance(next_page, dict):
                cursor = next_page.get("starting_after")
                if not cursor:
                    return
                if cursor == starting_after:
                    # A listing that cannot move forward must not look finished: the
                    # ids it never reached would be tombstoned.
                    raise IntercomSyncError(f"Intercom repeated a page cursor on {path}.")
                starting_after = cursor
                continue
            total_pages = pages.get("total_pages")
            # Numbered pages: stop when there is no next page, the last page is
            # reached, or a page comes back empty (no infinite loop).
            if not next_page or not items or (total_pages and page_number >= total_pages):
                return
            page_number += 1
        raise IntercomSyncError(f"Intercom listing of {path} exceeded {_MAX_PAGES} pages.")


def _parse(response: Any, path: str) -> dict:
    """Raise on a failed response with a hint for the common causes; else decode JSON."""
    if response.status_code in (401, 403):
        raise requests.HTTPError(
            f"Intercom refused {path} (HTTP {response.status_code}). Check that the access "
            "token is valid and the app has read permission for this content.",
            response=response,
        )
    response.raise_for_status()
    try:
        return response.json()
    except ValueError:
        raise IntercomSyncError(
            f"Intercom answered {path} with something other than JSON (HTTP "
            f"{response.status_code}); check base_url."
        ) from None


def _retry_delay(headers: Any, attempt: int) -> float:
    """Seconds to wait: Retry-After, else until X-RateLimit-Reset, else backoff."""
    headers = headers or {}
    retry_after = headers.get("Retry-After")
    if retry_after is not None:
        try:
            return min(float(retry_after), _MAX_RETRY_DELAY_SECONDS)
        except ValueError:
            pass
    reset = headers.get("X-RateLimit-Reset")
    if reset is not None:
        try:
            return min(max(float(reset) - time.time(), 0.0), _MAX_RETRY_DELAY_SECONDS)
        except ValueError:
            pass
    return min(2.0**attempt, _MAX_RETRY_DELAY_SECONDS)


def forget_all(
    state: dict, stats: dict | None = None, check_active: Callable[[], None] | None = None
) -> Iterator[dict]:
    """Tombstone every id a now-disabled resource synced before, then reset it."""
    stats = {} if stats is None else stats
    known_ids = state.get("known_ids", [])
    for doc_id in sorted(known_ids):
        yield _checked({"id": doc_id, "_deleted": True}, check_active)
    stats["deleted"] = stats.get("deleted", 0) + len(known_ids)
    if known_ids:
        logger.info("Intercom: content type disabled; forgetting %d document(s).", len(known_ids))
    state.clear()


# ---------------------------------------------------------------------------
# Conversations
# ---------------------------------------------------------------------------
def sync_conversations(
    api: IntercomAPI,
    state: dict,
    stats: dict | None = None,
    check_active: Callable[[], None] | None = None,
) -> Iterator[dict]:
    """Yield conversations changed since the cursor, then tombstones for deleted ones.

    The id sweep runs first, so a conversation created after it is still synced
    (and remembered in state), and one deleted after it 404s on retrieve and is
    skipped; the next run's sweep tombstones it.
    """
    stats = {} if stats is None else stats
    known_ids = set(state.get("known_ids", []))
    current_ids = _listed_ids(api.paginate("GET", "/conversations", "conversations"), stats)
    _refuse_empty_listing("conversations", current_ids, known_ids)

    cursor = state.get("cursor")
    since = max(cursor - _SEARCH_OVERLAP_SECONDS, 0) if cursor else 0
    query = {"query": {"field": "updated_at", "operator": ">", "value": since}}

    newest = cursor
    fetched_ids: set[str] = set()
    digests = dict(state.get("digests", {}))
    emitted = 0
    for summary in api.paginate("POST", "/conversations/search", "conversations", query):
        if not summary.get("id"):
            stats["failed"] = stats.get("failed", 0) + 1
            continue
        conversation_id = str(summary["id"])
        try:
            conversation = api.request(
                "GET", f"/conversations/{conversation_id}", params={"display_as": "plaintext"}
            )
        except requests.HTTPError as exc:
            if exc.response is not None and exc.response.status_code == 404:
                logger.warning("Intercom: conversation %s vanished, skipping.", conversation_id)
                continue
            raise
        fetched_ids.add(conversation_id)
        updated_at = summary.get("updated_at")
        if updated_at is not None and (newest is None or updated_at > newest):
            newest = updated_at
        row = conversation_to_row(conversation)
        if _unchanged(row, digests):
            continue  # re-read by the overlap window, or touched without a visible change
        emitted += 1
        yield _checked(row, check_active)

    deleted = known_ids - current_ids
    for conversation_id in sorted(deleted):
        digests.pop(conversation_id, None)
        yield _checked({"id": conversation_id, "_deleted": True}, check_active)
    _count(
        stats,
        scanned=len(current_ids),
        skipped=len(fetched_ids) - emitted,
        changed=emitted,
        deleted=len(deleted),
    )

    state["known_ids"] = sorted(current_ids | fetched_ids)
    state["digests"] = {doc_id: d for doc_id, d in digests.items() if doc_id in state["known_ids"]}
    if newest is not None:
        state["cursor"] = newest
    logger.info("Intercom: %d changed conversation(s), %d deletion(s).", emitted, len(deleted))


def conversation_to_row(conversation: dict) -> dict:
    """Flatten a conversation and its parts into a document row."""
    conversation_id = str(conversation["id"])
    source = conversation.get("source") or {}
    title = (
        conversation.get("title")
        or html_to_text(source.get("subject"))
        or f"Conversation {conversation_id}"
    )

    started = _format_date(conversation.get("created_at"))
    lines = [f"Intercom conversation {conversation_id}, started {started}."]
    if conversation.get("state"):
        lines[0] += f" State: {conversation['state']}."
    tags = (conversation.get("tags") or {}).get("tags") or []
    if tags:
        lines.append("Tags: " + ", ".join(sorted(tag["name"] for tag in tags)))

    opening = html_to_text(source.get("body"))
    if opening:
        lines.append(f"{_author(source.get('author'))}: {opening}")

    parts_page = conversation.get("conversation_parts") or {}
    parts = parts_page.get("conversation_parts") or []
    for part in parts:
        body = html_to_text(part.get("body"))
        if not body:
            continue  # assignment / state-change events carry no text
        label = " (internal note)" if part.get("part_type") == "note" else ""
        lines.append(f"{_author(part.get('author'))}{label}: {body}")

    total = parts_page.get("total_count") or 0
    if total > len(parts):
        logger.warning(
            "Intercom: conversation %s has %d parts; the API returned the %d most recent.",
            conversation_id,
            total,
            len(parts),
        )
        lines.append(f"(Only the {len(parts)} most recent of {total} messages are included.)")

    return {
        "id": conversation_id,
        "title": _clean(title),
        "content": _clean("\n\n".join(lines)),
        "_deleted": False,
    }


def _author(author: dict | None) -> str:
    """'Customer Asha Rao' / 'Agent Ben' / 'Bot' — role and name, never the email."""
    author = author or {}
    role = _AUTHOR_ROLES.get(author.get("type") or "", "Someone")
    name = (author.get("name") or "").strip()
    return f"{role} {name}" if name else role


# ---------------------------------------------------------------------------
# Help Center articles
# ---------------------------------------------------------------------------
def sync_articles(
    api: IntercomAPI,
    include_drafts: bool,
    state: dict,
    stats: dict | None = None,
    check_active: Callable[[], None] | None = None,
) -> Iterator[dict]:
    """Yield every (published) article, then tombstones for removed/unpublished ones."""
    stats = {} if stats is None else stats
    known_ids = set(state.get("known_ids", []))
    rows = []
    for page in range(1, _MAX_PAGES + 1):
        response = api.request(
            "GET", "/articles", params={"page": page, "per_page": _ARTICLES_PAGE_SIZE}
        )
        for article in response.get("data") or []:
            if not article.get("id"):
                stats["failed"] = stats.get("failed", 0) + 1
                continue
            if article.get("state") != "published" and not include_drafts:
                continue
            rows.append(article_to_row(article))
        if page >= ((response.get("pages") or {}).get("total_pages") or 1):
            break
    else:
        raise IntercomSyncError(f"Intercom article listing exceeded {_MAX_PAGES} pages.")
    current_ids = {row["id"] for row in rows}
    if state.get("include_drafts", include_drafts) == include_drafts:
        _refuse_empty_listing("articles", current_ids, known_ids)

    digests = dict(state.get("digests", {}))
    changed = [row for row in rows if not _unchanged(row, digests)]
    for row in changed:
        yield _checked(row, check_active)
    deleted = known_ids - current_ids
    for article_id in sorted(deleted):
        yield _checked({"id": article_id, "_deleted": True}, check_active)
    state["known_ids"] = sorted(current_ids)
    state["digests"] = {doc_id: d for doc_id, d in digests.items() if doc_id in current_ids}
    state["include_drafts"] = include_drafts
    _count(
        stats,
        scanned=len(current_ids),
        skipped=len(rows) - len(changed),
        changed=len(changed),
        deleted=len(deleted),
    )
    logger.info("Intercom: %d article(s), %d deletion(s).", len(current_ids), len(deleted))


def article_to_row(article: dict) -> dict:
    lines = []
    description = (article.get("description") or "").strip()
    if description:
        lines.append(description)
    body = html_to_text(article.get("body"))
    if body:
        lines.append(body)
    return {
        "id": str(article["id"]),
        "title": _clean(article.get("title") or f"Article {article['id']}"),
        "content": _clean("\n\n".join(lines)),
        "url": article.get("url"),
        "_deleted": False,
    }


# ---------------------------------------------------------------------------
# Contact notes (opt-in)
# ---------------------------------------------------------------------------
def sync_contact_notes(
    api: IntercomAPI,
    state: dict,
    stats: dict | None = None,
    check_active: Callable[[], None] | None = None,
) -> Iterator[dict]:
    """Yield every note on every contact, then tombstones for deleted notes."""
    stats = {} if stats is None else stats
    known_ids = set(state.get("known_ids", []))
    rows = []
    for contact in api.paginate("GET", "/contacts", "data"):
        if not contact.get("id"):
            stats["failed"] = stats.get("failed", 0) + 1
            continue
        for note in api.paginate("GET", f"/contacts/{contact['id']}/notes", "data"):
            if not note.get("id"):
                stats["failed"] = stats.get("failed", 0) + 1
                continue
            rows.append(contact_note_to_row(note, contact))
    current_ids = {row["id"] for row in rows}
    _refuse_empty_listing("contact notes", current_ids, known_ids)

    digests = dict(state.get("digests", {}))
    changed = [row for row in rows if not _unchanged(row, digests)]
    for row in changed:
        yield _checked(row, check_active)
    deleted = known_ids - current_ids
    for note_id in sorted(deleted):
        yield _checked({"id": note_id, "_deleted": True}, check_active)
    _count(
        stats,
        scanned=len(current_ids),
        skipped=len(rows) - len(changed),
        changed=len(changed),
        deleted=len(deleted),
    )
    state["known_ids"] = sorted(current_ids)
    state["digests"] = {doc_id: d for doc_id, d in digests.items() if doc_id in current_ids}
    logger.info("Intercom: %d contact note(s), %d deletion(s).", len(current_ids), len(deleted))


def contact_note_to_row(note: dict, contact: dict) -> dict:
    who = (contact.get("name") or "").strip() or f"contact {contact['id']}"
    author = _author({"type": "admin", **(note.get("author") or {})})
    lines = [
        f"Note on {who}, written {_format_date(note.get('created_at'))} by {author}.",
        html_to_text(note.get("body")),
    ]
    return {
        "id": str(note["id"]),
        "title": _clean(f"Note on {who}"),
        "content": _clean("\n\n".join(line for line in lines if line)),
        "_deleted": False,
    }


# ---------------------------------------------------------------------------
# Text helpers
# ---------------------------------------------------------------------------
def _listed_ids(items: Iterator[dict], stats: dict) -> set[str]:
    ids = set()
    for item in items:
        if item.get("id"):
            ids.add(str(item["id"]))
        else:
            stats["failed"] = stats.get("failed", 0) + 1
    return ids


def _refuse_empty_listing(kind: str, current_ids: set[str], known_ids: set[str]) -> None:
    """Stop instead of forgetting everything when a listing suddenly comes back empty."""
    if not current_ids and known_ids:
        raise IntercomSyncError(
            f"Intercom listed no {kind}, but the last run synced {len(known_ids)}. Refusing "
            f"to forget them; check the token's access. To really forget them, switch the "
            f"content type off."
        )


def _count(stats: dict, **counts: int) -> None:
    for key, value in counts.items():
        stats[key] = stats.get(key, 0) + value


def _unchanged(row: dict, digests: dict) -> bool:
    """Whether ``row`` renders exactly as last synced; records its hash either way."""
    digest = hashlib.sha256(json.dumps(row, sort_keys=True).encode()).hexdigest()
    same = digests.get(row["id"]) == digest
    digests[row["id"]] = digest
    return same


def _checked(row: dict, check_active: Callable[[], None] | None) -> dict:
    """Ask the host checkpoint before a row is published."""
    if check_active is not None:
        check_active()
    return row


def _clean(text: str) -> str:
    return _UNSAFE_CHARS.sub("", text)


def html_to_text(value: str | None) -> str:
    """Render Intercom's HTML bodies as plain text, keeping paragraph breaks."""
    if not value:
        return ""
    text = _BLOCK_END_RE.sub("\n", value)
    text = html.unescape(_TAG_RE.sub("", text))
    lines = (_SPACES_RE.sub(" ", line).strip() for line in text.splitlines())
    return "\n".join(line for line in lines if line)


def _format_date(timestamp: int | None) -> str:
    if not timestamp:
        return "at an unknown time"
    return datetime.fromtimestamp(timestamp, tz=timezone.utc).date().isoformat()
