"""DLT source for Readwise highlights (incremental sync + forget-on-delete).

Reads the Readwise export API (``GET /api/v2/export/``) and yields **one document
per highlight** (plus one per book-level note) for cognee's ingestion pipeline::

    import cognee
    from cognee_community_connector_readwise import readwise_source

    await cognee.remember(
        readwise_source(),               # READWISE_TOKEN from env, or token=...
        dataset_name="readwise",
        write_disposition="merge",       # REQUIRED (see .. important:: below)
    )

Highlights are ingested as *normal documents*: the source declares
``cognee_document_source = "readwise"``, so ``resolve_dlt_sources`` tags each row
``system_metadata["source"] = "readwise"`` and it flows through the standard
cognify entity-extraction pipeline (not the deterministic dlt-row path).

.. important::
   ``write_disposition="merge"`` is **mandatory**. The add pipeline defaults to
   ``"replace"`` (drop + reload the table each run); an incremental run only sees
   what changed, so ``replace`` would forget everything else.

Design
------
* **Auth** — a Readwise access token (https://readwise.io/access_token), sent as
  ``Authorization: Token <token>``. No OAuth.
* **Granularity** — one row per highlight, ``id = "highlight:{highlight_id}"``.
  Readwise returns only the highlights *updated* since the cursor, so a
  per-book document would be rebuilt from a partial set. A per-highlight row is
  self-contained: it carries the book title, author, source and link, plus the
  highlight text, your note and tags. A non-empty book-level note becomes its own
  row, ``id = "note:{user_book_id}"``.
* **Incremental cursor** — ``updatedAfter``. The first run backfills everything
  and records the UTC time the run *started*; later runs pass that time as
  ``updatedAfter`` and fetch only changed highlights. The cursor lives in dlt's
  per-resource state and only advances after a fully successful run, so a failed
  run is retried from the same point.
* **Forget-on-delete** — requests send ``includeDeleted=true``. Highlights (or
  whole books) Readwise reports as deleted are emitted as ``{"id", "_deleted":
  True}`` tombstones. dlt removes those rows on ``merge``, and cognee's existing
  ``orphan_cleanup`` then purges them from the graph and vector stores.

Limitations
-----------
Clearing a book-level note upstream does not remove the old ``note:`` row until
the book itself is deleted. Highlights have no such gap.
"""

from __future__ import annotations

import os
import time
from collections.abc import Iterable, Iterator
from datetime import UTC, datetime
from typing import Any

from cognee.shared.logging_utils import get_logger
from cognee.tasks.ingestion.dlt_utils import DOCUMENT_SOURCE_ATTR

logger = get_logger("readwise_connector")

READWISE_TABLE_NAME = "readwise_highlights"
READWISE_SOURCE_NAME = "readwise"
READWISE_EXPORT_URL = "https://readwise.io/api/v2/export/"

# Retry budget for rate-limited / transient Readwise responses. The export
# endpoint is limited to ~20 requests/minute and answers 429 + Retry-After.
_MAX_RETRIES = 5
_TRANSIENT_STATUS = frozenset({429, 500, 502, 503, 504})

_EXTRA_HINT = (
    "The Readwise connector requires dlt and httpx: pip install cognee-community-connector-readwise"
)


def readwise_source(
    token: str | None = None,
    updated_after: str | None = None,
    categories: Iterable[str] | None = None,
    book_ids: Iterable[int] | None = None,
    client: Any = None,
):
    """Create a dlt resource that yields Readwise highlights as documents.

    Args:
        token: Readwise access token. Falls back to ``READWISE_TOKEN``.
        updated_after: ISO 8601 timestamp. Only used on the first run (no saved
            cursor yet) to skip older highlights. Later runs use the saved cursor.
        categories: Only ingest these Readwise categories, e.g.
            ``["books", "articles"]``. Others are `tweets`, `podcasts` and
            `supplementals`. Default: all.
        book_ids: Only ingest these ``user_book_id`` values. Default: all.
        client: Pre-built ``httpx.Client``-like object (a test-injection point).
            It needs a ``get(url, params=...)`` method and must already carry the
            ``Authorization`` header. When omitted one is built from ``token``.

    Returns:
        A dlt resource for ``cognee.remember(..., write_disposition="merge")``.
    """
    try:
        import dlt
    except ImportError as exc:
        raise ImportError(_EXTRA_HINT) from exc

    if client is None:
        try:
            import httpx
        except ImportError as exc:
            raise ImportError(_EXTRA_HINT) from exc

        resolved_token = token or os.environ.get("READWISE_TOKEN")
        if not resolved_token:
            raise ValueError("Readwise access token required: pass token= or set READWISE_TOKEN.")
        client = httpx.Client(
            headers={"Authorization": f"Token {resolved_token}"},
            timeout=60.0,
        )

    wanted_categories = {c.lower() for c in categories} if categories else None
    ids_param = ",".join(str(i) for i in book_ids) if book_ids else None

    @dlt.resource(
        name=READWISE_TABLE_NAME,
        primary_key="id",
        write_disposition="merge",
        # _deleted is a boolean hard-delete marker: rows where it is True are
        # removed from the dlt destination on merge, which propagates the
        # deletion through cognee's orphan_cleanup.
        columns={"_deleted": {"data_type": "bool", "hard_delete": True}},
    )
    def readwise_highlights():
        state = dlt.current.resource_state()
        cursor = state.get("updated_after") or updated_after
        # Captured BEFORE fetching so a highlight edited mid-run is picked up
        # again next time instead of being missed.
        run_started = _utc_now_iso()

        yielded = deleted = 0
        for book in _iter_books(client, cursor, ids_param):
            if wanted_categories and (book.get("category") or "").lower() not in wanted_categories:
                continue
            for row in _book_to_rows(book):
                if row["_deleted"]:
                    deleted += 1
                else:
                    yielded += 1
                yield row

        # Reached only when every page was read: a failed run leaves the cursor
        # (and therefore memory) untouched and is simply retried.
        state["updated_after"] = run_started
        logger.info("Readwise: synced %d document(s), %d deletion(s).", yielded, deleted)

    resource = readwise_highlights
    # Opt into the document ingestion path (row -> text document -> cognify).
    # resolve_dlt_sources reads this marker; it never imports this connector.
    setattr(resource, DOCUMENT_SOURCE_ATTR, READWISE_SOURCE_NAME)
    return resource


# ---------------------------------------------------------------------------
# Readwise API helpers (module-private)
# ---------------------------------------------------------------------------


def _utc_now_iso() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _request(client: Any, params: dict) -> dict:
    """GET the export endpoint, retrying rate-limit / transient errors.

    Permanent errors (bad token, 4xx) and exhausted retries raise, so a partial
    read can never be mistaken for a complete one.
    """
    import httpx

    for attempt in range(_MAX_RETRIES):
        try:
            response = client.get(READWISE_EXPORT_URL, params=params)
        except httpx.TransportError as exc:
            if attempt == _MAX_RETRIES - 1:
                raise
            delay = float(2**attempt)
            logger.warning("Readwise: %s - retrying in %.1fs.", exc, delay)
            time.sleep(delay)
            continue

        status = response.status_code
        if status in _TRANSIENT_STATUS and attempt < _MAX_RETRIES - 1:
            delay = _retry_after(response.headers, attempt)
            logger.warning(
                "Readwise: HTTP %s - retrying in %.1fs (%d/%d).",
                status,
                delay,
                attempt + 1,
                _MAX_RETRIES,
            )
            time.sleep(delay)
            continue
        if status in (401, 403):
            raise PermissionError(
                f"Readwise rejected the access token (HTTP {status}). "
                "Check READWISE_TOKEN at https://readwise.io/access_token."
            )
        response.raise_for_status()
        return response.json()

    raise RuntimeError("Readwise: retry loop exited unexpectedly.")  # pragma: no cover


def _retry_after(headers: Any, attempt: int) -> float:
    """Seconds to wait: the Retry-After header, else exponential backoff."""
    header = None
    if headers:
        header = headers.get("Retry-After") or headers.get("retry-after")
    try:
        return max(float(header), 0.0)
    except (TypeError, ValueError):
        return float(2**attempt)


def _iter_books(client: Any, updated_after: str | None, ids: str | None) -> Iterator[dict]:
    """Yield exported books (each with its highlights) across all pages."""
    page_cursor: str | None = None
    seen_cursors: set[str] = set()
    while True:
        params: dict[str, Any] = {"includeDeleted": "true"}
        if updated_after:
            params["updatedAfter"] = updated_after
        if ids:
            params["ids"] = ids
        if page_cursor:
            params["pageCursor"] = page_cursor

        payload = _request(client, params)
        yield from payload.get("results") or []

        page_cursor = payload.get("nextPageCursor")
        # Stop on the last page, or if Readwise repeats a cursor (contract
        # violation) so we can never loop forever.
        if not page_cursor or page_cursor in seen_cursors:
            return
        seen_cursors.add(page_cursor)


def _book_to_rows(book: dict) -> Iterator[dict]:
    """Turn one exported book into highlight (and note) rows or tombstones."""
    book_id = book.get("user_book_id")
    book_deleted = bool(book.get("is_deleted"))

    for highlight in book.get("highlights") or []:
        row_id = f"highlight:{highlight.get('id')}"
        if book_deleted or highlight.get("is_deleted"):
            yield _tombstone(row_id)
        else:
            yield _highlight_to_row(book, highlight, row_id)

    note = (book.get("document_note") or "").strip()
    note_id = f"note:{book_id}"
    if book_deleted:
        yield _tombstone(note_id)
    elif note:
        yield {
            "id": note_id,
            "title": _book_title(book),
            "content": _join(_book_header(book), f"Note on the whole source: {note}"),
            "url": book.get("readwise_url") or None,
            "_deleted": False,
        }


def _tombstone(row_id: str) -> dict:
    return {"id": row_id, "_deleted": True}


def _highlight_to_row(book: dict, highlight: dict, row_id: str) -> dict:
    """Flatten a highlight + its book into a document row.

    Only ``title``/``content``/``url`` (+ ``id``) are kept, so a metadata-only
    change (``updated_at``, colour) does not churn the content-hash data_id.
    """
    parts = [_book_header(book), f"Highlight: {(highlight.get('text') or '').strip()}"]
    note = (highlight.get("note") or "").strip()
    if note:
        parts.append(f"My note: {note}")
    tags = [t.get("name") for t in highlight.get("tags") or [] if t.get("name")]
    if tags:
        parts.append("Tags: " + ", ".join(tags))
    return {
        "id": row_id,
        "title": _book_title(book),
        "content": _join(*parts),
        "url": highlight.get("readwise_url") or book.get("readwise_url") or None,
        "_deleted": False,
    }


def _book_title(book: dict) -> str:
    title = (book.get("readable_title") or book.get("title") or "").strip()
    author = (book.get("author") or "").strip()
    return f"{title} - {author}" if title and author else title or author


def _book_header(book: dict) -> str:
    """Provenance lines shared by every row of a book."""
    lines = []
    category = book.get("category")
    if category:
        lines.append(f"Type: {category}")
    source_url = book.get("source_url")
    if source_url:
        lines.append(f"Original source: {source_url}")
    book_tags = [t.get("name") for t in book.get("book_tags") or [] if t.get("name")]
    if book_tags:
        lines.append("Source tags: " + ", ".join(book_tags))
    summary = (book.get("summary") or "").strip()
    if summary:
        lines.append(f"Summary: {summary}")
    return "\n".join(lines)


def _join(*parts: str) -> str:
    return "\n\n".join(p for p in parts if p)
