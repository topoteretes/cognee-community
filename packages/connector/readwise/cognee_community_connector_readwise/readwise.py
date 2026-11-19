"""Readwise data-source connector for cognee — a ``dlt`` source over the Readwise Reader API.

Sync your Readwise library (books/articles + highlights + notes) into memory —
"ask my highlights".  Highlights are pre-curated — someone already decided each
one mattered — so they carry unusually high signal per token.

This builds entirely on the existing DLT ingestion subsystem; the source
produced here is meant to be handed directly to :func:`cognee.remember`::

    import cognee
    from cognee_community_connector_readwise import readwise_source

    await cognee.remember(
        readwise_source(),  # READWISE_API_KEY from env, or pass token=...
        dataset_name="readwise",
        write_disposition="merge",   # REQUIRED (see .. important:: below)
    )

.. important::
   ``write_disposition="merge"`` is **mandatory**.  The add pipeline defaults to
   ``"replace"`` (drop + reload the table each run); on the second, incremental
   sync that would wipe the entire synced library.  Always pass ``"merge"``.

Design
------
* **Auth** — Readwise access token (``Authorization: Token ...``).  Pass
  ``token=`` or set ``READWISE_API_KEY``.  Get one at
  https://readwise.io/access_token.  No OAuth dance.
* **What is ingested** — two document tables: ``readwise_books`` (one row per
  book/article source) and ``readwise_highlights`` (one row per highlight, with
  its note).  Rows are tagged ``cognee_document_source = "readwise"`` so they
  flow through cognee's normal cognify entity-extraction pipeline like the
  Notion connector, instead of the deterministic dlt-row path.
* **Primary key** — the Readwise ``id`` (stringified).  Combined with
  ``write_disposition="merge"`` this gives idempotent upserts.
* **Incremental cursor** — the highlights endpoint's ``updatedAfter`` filter.
  The first run does a full backfill and records the run time; subsequent runs
  fetch only highlights updated since, via the cursor persisted in dlt's source
  state.  Scope what you ingest with ``book_ids=[...]`` and/or
  ``category="books"`` / ``"articles"`` / ... .
* **Forget-on-delete** — the book listing is re-fetched every run (it is
  small).  A book that vanishes upstream is emitted with the ``_deleted``
  hard-delete marker, and every highlight previously seen under that book is
  emitted with ``_deleted`` too, so the whole source drops out of the graph on
  the next sync.  If a book's ``num_highlights`` shrinks, its highlights are
  re-listed and the missing ids are emitted as ``_deleted`` (catches
  individually-deleted highlights).  dlt removes those rows from its
  destination on ``merge``; they then fall out of the freshly read row set and
  cognee's existing ``orphan_cleanup`` purges them from the graph + vector +
  relational stores.  (Document-mode sources always read back the whole row
  set — ``resolve_dlt_sources`` hardcodes ``max_rows_per_table=0`` for them —
  so no extra flag is needed for cleanup to see the full corpus.)

Privacy
-------
This connector reads your reading highlights.  It is **opt-in**: nothing is
fetched until you explicitly construct a source and call ``remember``.  Use
``book_ids`` / ``category`` to scope what leaves Readwise, keep the access
token private, and prefer a dedicated dataset so you can ``cognee.forget`` the
library in one call.
"""

from __future__ import annotations

import os
import time
from collections.abc import Iterator
from datetime import datetime, timezone
from typing import Any, Optional

from cognee.shared.logging_utils import get_logger
from cognee.tasks.ingestion.dlt_utils import DOCUMENT_SOURCE_ATTR

logger = get_logger("readwise_connector")

READWISE_API_BASE = "https://readwise.io/api/v2"
READWISE_TABLE_BOOKS = "readwise_books"
READWISE_TABLE_HIGHLIGHTS = "readwise_highlights"
READWISE_SOURCE_NAME = "readwise"

READWISE_RATE_LIMIT_PER_MINUTE = 20

_MAX_RETRIES = 5
_BOOKS_PAGE_SIZE = 100
_HIGHLIGHTS_PAGE_SIZE = 1000  # the highlights endpoint returns up to 1000 per request

_EXTRA_HINT = (
    "This connector needs the 'readwise' extra: pip install cognee[readwise] "
    "(or cognee-community-connector-readwise)."
)


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------------------
# HTTP client
# ---------------------------------------------------------------------------
class ReadwiseClient:
    """Thin wrapper over the Readwise Reader API with pagination + retries.

    Only the two methods below are used by the sync functions, so tests inject
    a fake with the same interface.
    """

    def __init__(self, token: str) -> None:
        try:
            import httpx
        except ImportError as exc:
            raise ImportError(_EXTRA_HINT) from exc
        self._client = httpx.Client(
            base_url=READWISE_API_BASE,
            headers={"Authorization": f"Token {token}"},
            timeout=30.0,
        )
        self._check_token()

    def _check_token(self) -> None:
        # GET /api/v2/auth/ returns 204 for a valid token.
        resp = self._client.get("auth/")
        if resp.status_code == 204:
            return
        raise ValueError(
            "Readwise access token rejected (HTTP %s). "
            "Get a fresh token at https://readwise.io/access_token." % resp.status_code
        )

    def _get(self, path: str, params: Optional[dict] = None) -> dict:
        for attempt in range(_MAX_RETRIES):
            resp = self._client.get(path, params=params)
            if resp.status_code == 429 or resp.status_code >= 500:
                if attempt == _MAX_RETRIES - 1:
                    resp.raise_for_status()
                delay = _retry_delay(resp, attempt)
                logger.warning(
                    "Readwise API %s (attempt %d/%d); retrying in %.1fs",
                    resp.status_code,
                    attempt + 1,
                    _MAX_RETRIES,
                    delay,
                )
                time.sleep(delay)
                continue
            resp.raise_for_status()
            return resp.json()
        raise AssertionError("unreachable")  # pragma: no cover

    def list_books(self, *, category: Optional[str] = None) -> Iterator[dict]:
        """Yield every book/article; follows the numbered ``page`` pagination."""
        page = 1
        while True:
            params: dict = {"page_size": _BOOKS_PAGE_SIZE, "page": page}
            if category:
                params["category"] = category
            data = self._get("books/", params)
            yield from data.get("results", [])
            if not data.get("next"):
                return
            page += 1

    def list_highlights(
        self,
        *,
        updated_after: Optional[str] = None,
        book_id: Optional[str] = None,
    ) -> Iterator[dict]:
        """Yield highlights; follows the ``pageCursor`` pagination.

        ``updated_after`` is an ISO-8601 timestamp passed to Readwise's
        ``updatedAfter`` filter for incremental syncs.
        """
        params: dict = {"page_size": _HIGHLIGHTS_PAGE_SIZE}
        if updated_after:
            params["updatedAfter"] = updated_after
        if book_id:
            params["book_id"] = book_id
        cursor: Optional[str] = None
        while True:
            if cursor:
                params["pageCursor"] = cursor
            data = self._get("highlights/", params)
            yield from data.get("results", [])
            cursor = data.get("nextPageCursor")
            if not cursor:
                return


def _retry_delay(resp: Any, attempt: int) -> float:
    retry_after = resp.headers.get("Retry-After") if resp is not None else None
    if retry_after:
        try:
            return float(retry_after)
        except ValueError:
            pass
    # Exponential backoff with the rate-limit window (60s / 20 req) as the floor.
    return (60.0 / READWISE_RATE_LIMIT_PER_MINUTE) * (2**attempt)


# ---------------------------------------------------------------------------
# Row builders
# ---------------------------------------------------------------------------
def _deleted_row(item_id: Any) -> dict:
    return {"id": str(item_id), "_deleted": True}


def _book_to_row(book: dict) -> dict:
    """A book/article becomes a short "source" document."""
    title = book.get("title") or "Untitled"
    author = book.get("author") or ""
    category = book.get("category") or ""
    num_highlights = book.get("num_highlights") or 0
    lines = [title]
    if author:
        lines.append(f"by {author}")
    if category:
        lines.append(f"Category: {category}")
    lines.append(f"{num_highlights} highlight(s)")
    return {
        "id": str(book["id"]),
        "url": book.get("source_url") or book.get("highlights_url") or "",
        "title": title,
        "content": "\n".join(lines),
        "_deleted": False,
    }


def _highlight_to_row(highlight: dict, book: Optional[dict]) -> dict:
    """A highlight becomes a document: the text, its note, and the source."""
    text = (highlight.get("text") or "").strip()
    note = (highlight.get("note") or "").strip()
    book = book or {}
    book_title = book.get("title") or "Unknown source"
    author = book.get("author") or ""
    parts = [text]
    if note:
        parts.append(f"Note: {note}")
    tags = [
        t.get("name", "")
        for t in (highlight.get("tags") or [])
        if isinstance(t, dict) and t.get("name")
    ]
    return {
        "id": str(highlight["id"]),
        "url": highlight.get("highlight_url") or highlight.get("url") or "",
        "title": f"{book_title} — {author}" if author else book_title,
        "content": "\n\n".join(p for p in parts if p),
        "book_id": str(highlight.get("book_id")),
        "author": author,
        "category": book.get("category") or "",
        "tags": ", ".join(tags),
        "_deleted": False,
    }


# ---------------------------------------------------------------------------
# Sync functions (pure w.r.t. I/O: they take a client + a state dict)
# ---------------------------------------------------------------------------
def _scope(book_ids: Optional[list] = None) -> Optional[set]:
    return {str(b) for b in book_ids} if book_ids else None


def sync_books(
    client: Any,
    state: dict,
    *,
    category: Optional[str] = None,
    book_ids: Optional[list] = None,
) -> Iterator[dict]:
    """Full book listing every run; vanished books are emitted as ``_deleted``.

    The listing is small, so it doubles as the deletion-detection inventory.
    """
    scope = _scope(book_ids)
    seen = set(state.get("seen_book_ids", []))
    current: set[str] = set()
    for book in client.list_books(category=category):
        book_id = str(book["id"])
        if scope is not None and book_id not in scope:
            continue
        current.add(book_id)
        yield _book_to_row(book)
    for book_id in sorted(seen - current):
        yield _deleted_row(book_id)
    state["seen_book_ids"] = sorted(current)


def sync_highlights(
    client: Any,
    state: dict,
    *,
    book_ids: Optional[list] = None,
    category: Optional[str] = None,
) -> Iterator[dict]:
    """Incremental highlight sync with forget-on-delete.

    1. Re-fetch the book listing (needed for titles anyway); books that
       vanished since the last run cascade ``_deleted`` to their highlights.
    2. Fetch highlights with ``updatedAfter`` = the previous run's start time
       (full backfill when there is no cursor yet); skip discarded items.
    3. Reconcile books whose ``num_highlights`` shrank: re-list that book's
       highlights and emit ``_deleted`` for the ids that disappeared (catches
       individually-deleted highlights).

    Known edge: if a highlight is deleted *and* another added on the same book
    between runs (net ``num_highlights`` unchanged), the deletion is not
    detected until the book's count next changes or the book is deleted.  The
    Readwise API exposes no deletion feed, so absence is the only signal.
    """
    run_started = _utcnow_iso()
    scope = _scope(book_ids)

    books: dict[str, dict] = {}
    for book in client.list_books(category=category):
        book_id = str(book["id"])
        if scope is not None and book_id not in scope:
            continue
        books[book_id] = book

    books_state: dict = state.setdefault("books", {})
    deleted_ids: set[str] = set()

    # 1. Deleted books cascade to their highlights.
    for book_id in [b for b in books_state if b not in books]:
        deleted_ids.update(books_state[book_id].get("highlight_ids", []))
        del books_state[book_id]
    for highlight_id in sorted(deleted_ids):
        yield _deleted_row(highlight_id)

    # 2. Incremental delta via updatedAfter.
    last_sync = state.get("last_highlight_sync")
    fetched_ids: dict[str, set[str]] = {}
    for highlight in client.list_highlights(updated_after=last_sync):
        if highlight.get("is_discard"):
            continue
        book_id = str(highlight.get("book_id"))
        if book_id not in books:
            continue
        highlight_id = str(highlight["id"])
        fetched_ids.setdefault(book_id, set()).add(highlight_id)
        yield _highlight_to_row(highlight, books[book_id])

    # 3. Reconcile shrunk books (individually-deleted highlights).
    for book_id, book in books.items():
        bstate = books_state.setdefault(book_id, {})
        prev_ids = set(bstate.get("highlight_ids", []))
        prev_num = bstate.get("num_highlights")
        cur_num = book.get("num_highlights")
        if prev_num is not None and cur_num is not None and cur_num < prev_num:
            current_ids = {
                str(h["id"])
                for h in client.list_highlights(book_id=book_id)
                if not h.get("is_discard")
            }
            gone = prev_ids - current_ids
            for highlight_id in sorted(gone):
                yield _deleted_row(highlight_id)
            deleted_ids.update(gone)
            fetched_ids[book_id] = (
                fetched_ids.get(book_id, set()) | current_ids
            ) - gone

    # 4. Advance cursors.
    for book_id, book in books.items():
        bstate = books_state.setdefault(book_id, {})
        prev_ids = set(bstate.get("highlight_ids", []))
        bstate["highlight_ids"] = sorted(
            (prev_ids | fetched_ids.get(book_id, set())) - deleted_ids
        )
        bstate["num_highlights"] = book.get("num_highlights")
        bstate["updated"] = book.get("updated")
    state["last_highlight_sync"] = run_started


# ---------------------------------------------------------------------------
# dlt wiring
# ---------------------------------------------------------------------------
def readwise_source(
    *,
    token: Optional[str] = None,
    book_ids: Optional[list] = None,
    category: Optional[str] = None,
    client: Any = None,
):
    """Build the ``dlt`` source for a Readwise library.

    Args:
        token: Readwise access token.  Falls back to the ``READWISE_API_KEY``
            environment variable.  Get one at https://readwise.io/access_token.
        book_ids: Restrict ingestion to these Readwise book ids
            ("select what to ingest").
        category: Restrict to a book category, e.g. ``"books"``, ``"articles"``.
        client: Inject a client (tests).  Built from ``token`` when omitted.
    """
    try:
        import dlt
    except ImportError as exc:
        raise ImportError(_EXTRA_HINT) from exc

    if client is None:
        resolved = token or os.environ.get("READWISE_API_KEY")
        if not resolved:
            raise ValueError(
                "Readwise access token required: pass token= or set "
                "READWISE_API_KEY (get one at https://readwise.io/access_token)."
            )
        client = ReadwiseClient(resolved)

    @dlt.resource(
        name=READWISE_TABLE_BOOKS,
        primary_key="id",
        write_disposition="merge",
        columns={"_deleted": {"data_type": "bool", "hard_delete": True}},
    )
    def readwise_books():
        yield from sync_books(
            client,
            dlt.current.source_state(),
            category=category,
            book_ids=book_ids,
        )

    @dlt.resource(
        name=READWISE_TABLE_HIGHLIGHTS,
        primary_key="id",
        write_disposition="merge",
        columns={"_deleted": {"data_type": "bool", "hard_delete": True}},
    )
    def readwise_highlights():
        yield from sync_highlights(
            client,
            dlt.current.source_state(),
            book_ids=book_ids,
            category=category,
        )

    @dlt.source(name=READWISE_SOURCE_NAME)
    def _readwise():
        return readwise_books, readwise_highlights

    source = _readwise()
    # Route rows through cognee's normal document ingestion (cognify), like the
    # Notion connector, instead of the deterministic dlt-row path.
    setattr(source, DOCUMENT_SOURCE_ATTR, READWISE_SOURCE_NAME)
    return source
