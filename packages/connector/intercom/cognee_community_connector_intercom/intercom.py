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
from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any

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
_REQUEST_TIMEOUT_SECONDS = 30

_EXTRA_HINT = (
    'The Intercom connector requires requests and dlt: pip install requests "dlt[sqlalchemy]"'
)

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
        session: Pre-built ``requests`` session (mainly a test-injection point);
            when omitted one is built from the token.

    Returns:
        A dlt source for ``cognee.remember(..., write_disposition="merge")``.
    """
    try:
        import dlt
    except ImportError as exc:
        raise ImportError(_EXTRA_HINT) from exc

    if session is None:
        resolved_token = token or os.environ.get("INTERCOM_ACCESS_TOKEN")
        if not resolved_token:
            raise ValueError(
                "Intercom access token required: pass token= or set INTERCOM_ACCESS_TOKEN."
            )
        session = _make_session(resolved_token)
    api = IntercomAPI(session, base_url)

    @dlt.resource(
        name=CONVERSATIONS_TABLE_NAME,
        primary_key="id",
        write_disposition="merge",
        columns=_TOMBSTONE_COLUMNS,
    )
    def intercom_conversations():
        yield from sync_conversations(api, dlt.current.resource_state())

    @dlt.resource(
        name=ARTICLES_TABLE_NAME,
        primary_key="id",
        write_disposition="merge",
        columns=_TOMBSTONE_COLUMNS,
    )
    def intercom_articles():
        yield from sync_articles(api, include_draft_articles, dlt.current.resource_state())

    @dlt.resource(
        name=CONTACT_NOTES_TABLE_NAME,
        primary_key="id",
        write_disposition="merge",
        columns=_TOMBSTONE_COLUMNS,
    )
    def intercom_contact_notes():
        yield from sync_contact_notes(api, dlt.current.resource_state())

    selected = [
        resource
        for resource, enabled in (
            (intercom_conversations, include_conversations),
            (intercom_articles, include_articles),
            (intercom_contact_notes, include_contact_notes),
        )
        if enabled
    ]
    if not selected:
        raise ValueError("Nothing to sync: enable conversations, articles or contact notes.")

    @dlt.source(name=INTERCOM_SOURCE_NAME)
    def _intercom():
        return selected

    source = _intercom()
    # Opt into the document ingestion path (row → text document → cognify).
    # resolve_dlt_sources reads this marker; it never imports this connector.
    setattr(source, DOCUMENT_SOURCE_ATTR, INTERCOM_SOURCE_NAME)
    # One dlt state (cursor, known ids) per workspace region and cognee dataset,
    # so syncing other sources or datasets on the same machine can't reset it.
    setattr(source, PIPELINE_SCOPE_ATTR, f"{INTERCOM_SOURCE_NAME}:{api.base_url}")
    return source


def _make_session(token: str) -> Any:
    try:
        import requests
    except ImportError as exc:
        raise ImportError(_EXTRA_HINT) from exc

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

    def __init__(self, session: Any, base_url: str):
        self.session = session
        self.base_url = base_url.rstrip("/")

    def request(
        self, method: str, path: str, params: dict | None = None, body: dict | None = None
    ) -> dict:
        """Call the API, retrying 429/5xx and network errors; other errors raise.

        A 429 waits for ``Retry-After`` or Intercom's ``X-RateLimit-Reset``; other
        retries back off exponentially. A permanent error (auth, not found) or an
        exhausted retry budget raises, which aborts the sync before any deletion.
        """
        import requests

        url = f"{self.base_url}{path}"
        attempt = 0
        while True:
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
                    response.raise_for_status()
                    return response.json()
                reason = f"HTTP {response.status_code}"
                delay = _retry_delay(response.headers, attempt)
            logger.warning("Intercom: %s on %s — retrying in %.1fs.", reason, path, delay)
            time.sleep(delay)
            attempt += 1

    def cursor_pages(
        self, method: str, path: str, key: str, body: dict | None = None
    ) -> Iterator[dict]:
        """Yield the items under ``key`` across ``starting_after`` cursor pagination."""
        starting_after = None
        while True:
            if method == "POST":
                pagination: dict[str, Any] = {"per_page": _PAGE_SIZE}
                if starting_after:
                    pagination["starting_after"] = starting_after
                page = self.request("POST", path, body={**(body or {}), "pagination": pagination})
            else:
                params: dict[str, Any] = {"per_page": _PAGE_SIZE}
                if starting_after:
                    params["starting_after"] = starting_after
                page = self.request("GET", path, params=params)
            yield from page.get(key) or []
            next_page = (page.get("pages") or {}).get("next")
            cursor = next_page.get("starting_after") if isinstance(next_page, dict) else None
            # Stop on the last page, or if the API repeats a cursor (no infinite loop).
            if not cursor or cursor == starting_after:
                return
            starting_after = cursor


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


# ---------------------------------------------------------------------------
# Conversations
# ---------------------------------------------------------------------------
def sync_conversations(api: IntercomAPI, state: dict) -> Iterator[dict]:
    """Yield conversations changed since the cursor, then tombstones for deleted ones.

    The id sweep runs first, so a conversation created after it is still synced
    (and remembered in state), and one deleted after it 404s on retrieve and is
    skipped; the next run's sweep tombstones it.
    """
    from requests import HTTPError

    known_ids = set(state.get("known_ids", []))
    current_ids = {
        str(item["id"]) for item in api.cursor_pages("GET", "/conversations", "conversations")
    }

    cursor = state.get("cursor")
    since = max(cursor - _SEARCH_OVERLAP_SECONDS, 0) if cursor else 0
    query = {"query": {"field": "updated_at", "operator": ">", "value": since}}

    newest = cursor
    fetched_ids: set[str] = set()
    for summary in api.cursor_pages("POST", "/conversations/search", "conversations", query):
        conversation_id = str(summary["id"])
        try:
            conversation = api.request(
                "GET", f"/conversations/{conversation_id}", params={"display_as": "plaintext"}
            )
        except HTTPError as exc:
            if exc.response is not None and exc.response.status_code == 404:
                logger.warning("Intercom: conversation %s vanished, skipping.", conversation_id)
                continue
            raise
        fetched_ids.add(conversation_id)
        if newest is None or summary["updated_at"] > newest:
            newest = summary["updated_at"]
        yield conversation_to_row(conversation)

    deleted = known_ids - current_ids
    for conversation_id in sorted(deleted):
        yield {"id": conversation_id, "_deleted": True}

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
def sync_articles(api: IntercomAPI, include_drafts: bool, state: dict) -> Iterator[dict]:
    """Yield every (published) article, then tombstones for removed/unpublished ones."""
    known_ids = set(state.get("known_ids", []))
    current_ids: set[str] = set()

    page = 1
    while True:
        response = api.request(
            "GET", "/articles", params={"page": page, "per_page": _ARTICLES_PAGE_SIZE}
        )
        for article in response.get("data") or []:
            if article.get("state") != "published" and not include_drafts:
                continue
            row = article_to_row(article)
            current_ids.add(row["id"])
            yield row
        if page >= ((response.get("pages") or {}).get("total_pages") or 1):
            break
        page += 1

    deleted = known_ids - current_ids
    for article_id in sorted(deleted):
        yield {"id": article_id, "_deleted": True}
    state["known_ids"] = sorted(current_ids)
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
def sync_contact_notes(api: IntercomAPI, state: dict) -> Iterator[dict]:
    """Yield every note on every contact, then tombstones for deleted notes."""
    known_ids = set(state.get("known_ids", []))
    current_ids: set[str] = set()

    for contact in api.cursor_pages("GET", "/contacts", "data"):
        for note in api.cursor_pages("GET", f"/contacts/{contact['id']}/notes", "data"):
            row = contact_note_to_row(note, contact)
            current_ids.add(row["id"])
            yield row

    deleted = known_ids - current_ids
    for note_id in sorted(deleted):
        yield {"id": note_id, "_deleted": True}
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
