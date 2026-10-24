"""Intercom connector for cognee — a ``dlt`` source that turns support history into memory.

Sync Intercom conversations (with their replies and internal notes), Help Center
articles and, opt-in, contact notes into cognee, incrementally and with
forget-on-delete::

    import cognee
    from cognee_community_connector_intercom import intercom_source

    await cognee.remember(
        intercom_source(),              # INTERCOM_ACCESS_TOKEN from the env, or token=...
        dataset_name="intercom",
        write_disposition="merge",     # REQUIRED (see .. important:: below)
    )

.. important::
   ``write_disposition="merge"`` is **mandatory**. cognee applies the caller's
   write disposition to every table of the source; conversations sync
   incrementally (unchanged ones are not re-emitted) and deletions are emitted
   as ``_deleted`` tombstone rows, both of which only work under ``merge``.

Design
------
* **Auth** — an access token from an Intercom app (Developer Hub → your app →
  Authentication), sent as a Bearer token. ``Intercom-Version`` is pinned so API
  changes cannot silently alter parsing. ``base_url`` selects the data region
  (US by default; ``https://api.eu.intercom.io`` or ``https://api.au.intercom.io``).
  The connector only issues reads (``GET``, plus ``POST`` to the search endpoint).
* **Conversations** — one document per conversation: its title or subject, the
  opening message and every reply and internal note in order, each labelled with
  the author's role and name. Email addresses are left out. Internal notes stay
  in the conversation they explain. Parts without text (assignments, state
  changes) are skipped.
* **Help Center articles** — one document per published article (drafts are
  opt-in); the HTML body is rendered to text.
* **Contact notes** — opt-in (``include_contact_notes=True``); one document per
  note. This walks every contact, one request each, so it suits small workspaces.
* **Incremental sync** — conversations come from the search endpoint filtered on
  ``updated_at``, starting five minutes before the newest ``updated_at`` seen
  last run (the search index can lag slightly behind writes). The cursor is kept
  in dlt's per-resource state. Articles and contact notes are re-read in full
  each run; unchanged ones keep a stable content hash and are not re-cognified.
* **Forget-on-delete** — Intercom has no delete feed, so each run sweeps the ids
  that currently exist (a list of all conversation ids, or the full article and
  note listings) and emits ids that vanished since the previous run with the
  ``_deleted`` hard-delete marker, like the Confluence connector. dlt drops them
  on ``merge`` and cognee's orphan cleanup forgets them. Any failed request
  aborts the run, so a broken listing never looks like a deletion.

Limitations
-----------
* Intercom returns at most the 500 most recent parts of a conversation; longer
  conversations are truncated (logged, and noted in the document).
* The conversation id sweep pages through every conversation each run (150 per
  request).
"""

from __future__ import annotations

import html
import os
import re
import time
from collections.abc import Callable, Iterator
from datetime import UTC, datetime
from typing import Any

import dlt
import requests
from cognee.shared.logging_utils import get_logger
from cognee.tasks.ingestion.dlt_utils import DOCUMENT_SOURCE_ATTR, PIPELINE_SCOPE_ATTR

logger = get_logger("intercom_connector")

INTERCOM_SOURCE_NAME = "intercom"
CONVERSATIONS_TABLE_NAME = "intercom_conversations"
ARTICLES_TABLE_NAME = "intercom_articles"
CONTACT_NOTES_TABLE_NAME = "intercom_contact_notes"

DEFAULT_BASE_URL = "https://api.intercom.io"
# Pin the API version so upstream changes can't silently alter parsing.
_INTERCOM_VERSION = "2.16"

# Intercom's maximum page size for cursor-paginated endpoints.
_PAGE_SIZE = 150
_ARTICLES_PAGE_SIZE = 50
# Re-query this far before the cursor: the search index can trail recent writes.
_SEARCH_OVERLAP_SECONDS = 300
# Retry budget for rate-limited / transient responses, and the longest single wait.
_MAX_RETRIES = 5
_MAX_RETRY_DELAY_SECONDS = 60.0
_MAX_PAGES = 100_000
_STAT_KEYS = ("scanned", "changed", "deleted", "failed", "requests")
_REQUEST_TIMEOUT_SECONDS = 30


_TOMBSTONE_COLUMNS = {"_deleted": {"data_type": "bool", "hard_delete": True}}

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
    check_active: Callable[[], None] | None = None,
    service: Any = None,
    session: Any = None,
):
    """Create a dlt source that yields Intercom content as documents.

    Args:
        token: Intercom access token. Falls back to ``INTERCOM_ACCESS_TOKEN``.
        base_url: API host for the workspace's data region.
        include_conversations: Sync conversations with their replies and notes.
        include_articles: Sync Help Center articles.
        include_draft_articles: Also sync unpublished articles.
        include_contact_notes: Also sync notes attached to contacts.
        check_active: Optional host checkpoint, called before every request and before
            every row is published; raising from it stops the run.
        service: Pre-built ``requests``-compatible session (tests, proxies). It is
            used as is and not closed; when omitted, each run opens and closes its own
            from the token.
        session: Alias of ``service``. Give at most one.

    Returns:
        A dlt source for ``cognee.remember(..., write_disposition="merge")``. After a
        run, ``source.cognee_sync_stats`` holds counts only, summed over the three
        content types: ``scanned``, ``changed``, ``deleted``, ``failed`` (items
        without an id), ``requests``, and ``complete`` (1 once all three finished).
    """
    if service is not None and session is not None:
        raise ValueError("Pass service= or session=, not both.")
    client = service if service is not None else session
    secret = _check_token(token if token is not None else os.environ.get("INTERCOM_ACCESS_TOKEN"))
    if client is None and not secret:
        raise ValueError(
            "Intercom access token required: pass token= or set INTERCOM_ACCESS_TOKEN."
        )
    base = base_url.rstrip("/")
    stats: dict[str, int] = {}
    run = _RunStats(stats, ("conversations", "articles", "contact_notes"))

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
            yield from sync(api, state, part, check_active)
        finally:
            if client is None:
                http.close()
        run.finish(name)

    @dlt.resource(
        name=CONVERSATIONS_TABLE_NAME,
        primary_key="id",
        write_disposition="merge",
        columns=_TOMBSTONE_COLUMNS,
    )
    def intercom_conversations():
        yield from _run("conversations", sync_conversations, include_conversations)

    @dlt.resource(
        name=ARTICLES_TABLE_NAME,
        primary_key="id",
        write_disposition="merge",
        columns=_TOMBSTONE_COLUMNS,
    )
    def intercom_articles():
        def sync(api, state, part, check):
            return sync_articles(api, include_draft_articles, state, part, check)

        yield from _run("articles", sync, include_articles)

    @dlt.resource(
        name=CONTACT_NOTES_TABLE_NAME,
        primary_key="id",
        write_disposition="merge",
        columns=_TOMBSTONE_COLUMNS,
    )
    def intercom_contact_notes():
        yield from _run("contact_notes", sync_contact_notes, include_contact_notes)

    if not (include_conversations or include_articles or include_contact_notes):
        raise ValueError("Nothing to sync: enable conversations, articles or contact notes.")

    @dlt.source(name=INTERCOM_SOURCE_NAME)
    def _intercom():
        # All three resources always run: a disabled one only tombstones what it
        # synced before, so switching content off forgets it instead of leaving
        # stale documents in memory.
        return [intercom_conversations, intercom_articles, intercom_contact_notes]

    source = _intercom()
    # Opt into the document ingestion path (row → text document → cognify).
    # resolve_dlt_sources reads this marker; it never imports this connector.
    setattr(source, DOCUMENT_SOURCE_ATTR, INTERCOM_SOURCE_NAME)
    # One dlt state (cursor, known ids) per workspace region and cognee dataset,
    # so syncing other sources or datasets on the same machine can't reset it.
    setattr(source, PIPELINE_SCOPE_ATTR, f"{INTERCOM_SOURCE_NAME}:{base}")
    source.cognee_sync_stats = stats
    return source


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
        self._publish()
        return part

    def finish(self, name: str) -> None:
        self.parts[name]["complete"] = 1
        self._publish()

    def _publish(self) -> None:
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

    def request(
        self, method: str, path: str, params: dict | None = None, body: dict | None = None
    ) -> dict:
        """Call the API, retrying 429/5xx and network errors; other errors raise.

        A 429 waits for ``Retry-After`` or Intercom's ``X-RateLimit-Reset``; other
        retries back off exponentially. A permanent error (auth, not found) or an
        exhausted retry budget raises, which aborts the sync before any deletion.
        """
        url = f"{self.base_url}{path}"
        attempt = 0
        while True:
            if self.check_active is not None:
                self.check_active()
            self.stats["requests"] = self.stats.get("requests", 0) + 1
            try:
                response = self.session.request(
                    method, url, params=params, json=body, timeout=_REQUEST_TIMEOUT_SECONDS
                )
            except (requests.ConnectionError, requests.Timeout) as exc:
                if attempt == _MAX_RETRIES - 1:
                    raise
                reason, delay = str(exc), min(2.0**attempt, _MAX_RETRY_DELAY_SECONDS)
            else:
                retryable = response.status_code == 429 or response.status_code >= 500
                if not retryable or attempt == _MAX_RETRIES - 1:
                    return _parse(response, path)
                reason = f"HTTP {response.status_code}"
                delay = _retry_delay(response.headers, attempt)
            logger.warning("Intercom: %s on %s — retrying in %.1fs.", reason, path, delay)
            time.sleep(delay)
            attempt += 1

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
    if response.status_code == 401:
        raise requests.HTTPError(
            f"Intercom rejected the access token on {path} (HTTP 401).", response=response
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
        yield _checked(conversation_to_row(conversation), check_active)

    deleted = known_ids - current_ids
    for conversation_id in sorted(deleted):
        yield _checked({"id": conversation_id, "_deleted": True}, check_active)
    _count(stats, scanned=len(current_ids), changed=len(fetched_ids), deleted=len(deleted))

    state["known_ids"] = sorted(current_ids | fetched_ids)
    if newest is not None:
        state["cursor"] = newest
    logger.info(
        "Intercom: %d changed conversation(s), %d deletion(s).", len(fetched_ids), len(deleted)
    )


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
        "title": title,
        "content": "\n\n".join(lines),
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

    for row in rows:
        yield _checked(row, check_active)
    deleted = known_ids - current_ids
    for article_id in sorted(deleted):
        yield _checked({"id": article_id, "_deleted": True}, check_active)
    state["known_ids"] = sorted(current_ids)
    state["include_drafts"] = include_drafts
    _count(stats, scanned=len(current_ids), changed=len(current_ids), deleted=len(deleted))
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
        "title": article.get("title") or f"Article {article['id']}",
        "content": "\n\n".join(lines),
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

    for row in rows:
        yield _checked(row, check_active)
    deleted = known_ids - current_ids
    for note_id in sorted(deleted):
        yield _checked({"id": note_id, "_deleted": True}, check_active)
    _count(stats, scanned=len(current_ids), changed=len(current_ids), deleted=len(deleted))
    state["known_ids"] = sorted(current_ids)
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
        "title": f"Note on {who}",
        "content": "\n\n".join(line for line in lines if line),
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


def _checked(row: dict, check_active: Callable[[], None] | None) -> dict:
    """Ask the host checkpoint before a row is published."""
    if check_active is not None:
        check_active()
    return row


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
    return datetime.fromtimestamp(timestamp, tz=UTC).date().isoformat()
