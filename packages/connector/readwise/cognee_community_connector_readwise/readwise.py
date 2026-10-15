"""DLT source for Readwise highlights (incremental sync + forget-on-delete).

Fetches Readwise highlights, notes, and their source books/articles using the
Readwise v2 REST API, then yields them as a dlt resource for cognee's ingestion
pipeline.

Unlike the relational dlt path (SQL/CSV), documents are ingested in *document
mode*: the source declares ``cognee_document_source = "readwise"``, so
``resolve_dlt_sources`` tags each row ``external_metadata["source"] = "readwise"``
(not ``"dlt"``). Each document flows through the standard cognify
entity-extraction pipeline, not the deterministic dlt-row schema-context path.

Sync strategy
-------------
*  **Incremental merge** — Readwise exposes an ``updatedAfter`` filter on both
   the highlights and books endpoints.  Each sync fetches only records modified
   since the last successful run.  The cursor is saved per resource in dlt
   state and advanced only after successful ingestion.

*  **Forget-on-delete** — Readwise has no delete feed.  The connector performs
   a lightweight ID reconciliation each sync: IDs present in the previous
   snapshot but absent from the current full listing are tombstoned
   (``_deleted=True``).  To keep the reconciliation cheap, only IDs (not full
   content) are fetched for the snapshot comparison.

Document model
--------------
Each Readwise *book* (book, article, tweet, podcast episode, …) becomes one
document containing its title, author, source URL, category, and all of its
highlights and notes concatenated in reading order.  Grouping highlights with
their source gives cognee the context to answer questions like
"What did I highlight about attention mechanisms?" or
"Summarise my notes on Deep Work."

Rate limits
-----------
The highlights and books LIST endpoints are capped at 20 requests/minute.
The ``Retry-After`` header on 429 responses is honoured, with bounded
exponential backoff for transient 5xx / network failures.
"""

import os
import time
from datetime import datetime, timezone
from typing import Any, Iterator

from cognee.shared.logging_utils import get_logger
from cognee.tasks.ingestion.dlt_utils import DOCUMENT_SOURCE_ATTR

logger = get_logger("readwise_connector")

# ──────────────────────────────────────────────────────────────────────────────
# Constants
# ──────────────────────────────────────────────────────────────────────────────

READWISE_SOURCE_NAME = "readwise"
_TABLE_BOOKS = "readwise_books"

_API_BASE = "https://readwise.io/api/v2"
_MAX_RETRIES = 5
_PAGE_SIZE = 1000  # Readwise returns up to 1000 highlights per page

_EXTRA_HINT = (
    "The Readwise connector requires dlt and httpx:\n"
    "  pip install cognee-community-connector-readwise\n"
)

# Readwise book categories
_CATEGORY_LABELS = {
    "books": "Book",
    "articles": "Article",
    "tweets": "Tweet",
    "supplementals": "Supplemental",
    "podcasts": "Podcast",
}


# ──────────────────────────────────────────────────────────────────────────────
# Public entry-point
# ──────────────────────────────────────────────────────────────────────────────


def readwise_source(
    token: str | None = None,
    categories: list[str] | None = None,
    http_client: Any = None,
):
    """Create a dlt source that yields Readwise books + highlights as documents.

    Args:
        token: Readwise access token. Falls back to ``READWISE_TOKEN`` env var.
            Get yours at https://readwise.io/access_token
        categories: Restrict ingestion to these categories. Allowed values:
            ``"books"``, ``"articles"``, ``"tweets"``, ``"supplementals"``,
            ``"podcasts"``. When omitted, all categories are ingested.
        http_client: Injectable ``_ReadwiseClient`` for tests; when omitted,
            one is built from the token above.

    Returns:
        A dlt source suitable for ``cognee.remember(...)``.
    """
    try:
        import dlt  # noqa: F401
    except ImportError as exc:
        raise ImportError(_EXTRA_HINT) from exc

    resolved_token = token or os.environ.get("READWISE_TOKEN")
    if not resolved_token:
        raise ValueError(
            "Readwise token required: pass token= or set READWISE_TOKEN.\n"
            "Get yours at https://readwise.io/access_token"
        )

    client = http_client or _ReadwiseClient(resolved_token)
    return _build_source(client=client, categories=categories)


# ──────────────────────────────────────────────────────────────────────────────
# DLT source / resource
# ──────────────────────────────────────────────────────────────────────────────


def _build_source(*, client: Any, categories: list[str] | None):
    import dlt

    @dlt.resource(
        name=_TABLE_BOOKS,
        primary_key="id",
        write_disposition={
            "disposition": "merge",
            "strategy": "upsert",
        },
        columns={"_deleted": {"data_type": "bool", "hard_delete": True}},
    )
    def readwise_books(dlt_state=dlt.state()):
        yield from _sync_books(client, categories, dlt_state)

    @dlt.source(name=READWISE_SOURCE_NAME)
    def _readwise():
        return readwise_books

    source = _readwise()
    setattr(source, DOCUMENT_SOURCE_ATTR, READWISE_SOURCE_NAME)
    return source


# ──────────────────────────────────────────────────────────────────────────────
# Core sync logic
# ──────────────────────────────────────────────────────────────────────────────


def _sync_books(
    client: Any,
    categories: list[str] | None,
    state: dict,
) -> Iterator[dict]:
    """Yield book documents with embedded highlights; tombstone deleted books."""

    book_state: dict = state.setdefault("books", {})
    # cursor is an ISO-8601 string; None means first run (full sync)
    cursor: str | None = book_state.get("cursor")

    # ── Fetch all books updated since last cursor ─────────────────────────────
    try:
        updated_books = list(_iter_books(client, updated_after=cursor, categories=categories))
    except Exception as exc:
        logger.error("Readwise: failed to fetch books: %s", exc)
        raise

    # ── For each updated book fetch its highlights and render a document ──────
    new_cursor = _utcnow_iso()
    known_ids: set[str] = set(book_state.get("known_ids", []))
    updated_ids: set[str] = set()

    for book in updated_books:
        book_id = str(book["id"])
        updated_ids.add(book_id)
        known_ids.add(book_id)

        try:
            highlights = list(_iter_highlights(client, book_id=book["id"]))
        except Exception as exc:
            logger.warning("Readwise: failed to fetch highlights for book %s: %s", book_id, exc)
            raise

        doc = _render_book_doc(book, highlights)
        logger.debug("Readwise: synced book %s (%s highlights)", book_id, len(highlights))
        yield doc

    # ── Forget-on-delete: reconcile full ID set on first run ──────────────────
    # On subsequent runs only do a full reconciliation if cursor existed,
    # meaning we might have missed deletions since last sync.
    if cursor is not None:
        try:
            current_ids = {str(b["id"]) for b in _iter_books(client, categories=categories, ids_only=True)}
        except Exception as exc:
            # Safety: never tombstone on a failed listing
            logger.warning("Readwise: ID reconciliation failed, skipping tombstones: %s", exc)
            current_ids = known_ids  # treat as unchanged

        for old_id in list(known_ids):
            if old_id not in current_ids:
                logger.info("Readwise: book %s deleted upstream — tombstoning", old_id)
                yield {"id": old_id, "_deleted": True}
                known_ids.discard(old_id)

    # Advance state only after successful processing
    book_state["cursor"] = new_cursor
    book_state["known_ids"] = list(known_ids)
    logger.info(
        "Readwise: synced %d book(s), total known: %d.", len(updated_ids), len(known_ids)
    )


# ──────────────────────────────────────────────────────────────────────────────
# API iteration helpers
# ──────────────────────────────────────────────────────────────────────────────


def _iter_books(
    client: Any,
    updated_after: str | None = None,
    categories: list[str] | None = None,
    ids_only: bool = False,
) -> Iterator[dict]:
    """Yield book objects from the Readwise /books/ endpoint."""
    params: dict[str, Any] = {"page_size": _PAGE_SIZE}
    if updated_after:
        params["updated__gt"] = updated_after
    if categories:
        # Readwise accepts a single category per request; iterate per category
        for cat in categories:
            params_cat = {**params, "category": cat}
            yield from _paginate(client, "/books/", params_cat)
        return
    yield from _paginate(client, "/books/", params)


def _iter_highlights(client: Any, book_id: int) -> Iterator[dict]:
    """Yield highlight objects for a single book."""
    params: dict[str, Any] = {"book_id": book_id, "page_size": _PAGE_SIZE}
    yield from _paginate(client, "/highlights/", params)


def _paginate(client: Any, path: str, params: dict) -> Iterator[dict]:
    """Follow Readwise's cursor-based pagination (next URL in response)."""
    url: str | None = path
    while url:
        resp = client.get(url, params=params if url == path else None)
        results = resp.get("results", [])
        yield from results
        url = resp.get("next")
        # next is a full URL on subsequent pages; clear params so they aren't duplicated
        params = {}


# ──────────────────────────────────────────────────────────────────────────────
# Document rendering
# ──────────────────────────────────────────────────────────────────────────────


def _render_book_doc(book: dict, highlights: list[dict]) -> dict:
    """Render a book + its highlights into a single cognee document row."""
    book_id = str(book["id"])
    title = book.get("title") or "(untitled)"
    author = book.get("author") or ""
    source_url = book.get("source_url") or book.get("unique_url") or ""
    category = _CATEGORY_LABELS.get(book.get("category", ""), book.get("category", ""))
    num_highlights = book.get("num_highlights", len(highlights))
    last_highlight = book.get("last_highlight_at") or ""
    cover_image = book.get("cover_image_url") or ""
    tags = _extract_tags(book)

    # Header section
    lines = [
        f"# {title}",
        f"Author: {author}" if author else "",
        f"Category: {category}",
        f"Highlights: {num_highlights}",
        f"Last highlighted: {last_highlight}" if last_highlight else "",
        f"Source: {source_url}" if source_url else "",
        f"Tags: {', '.join(tags)}" if tags else "",
        "",
    ]
    lines = [l for l in lines if l]  # drop empty lines

    # Highlights section — sorted by reading position (location)
    sorted_hl = sorted(highlights, key=lambda h: h.get("location") or 0)
    if sorted_hl:
        lines.append("## Highlights")
        for hl in sorted_hl:
            text = (hl.get("text") or "").strip()
            note = (hl.get("note") or "").strip()
            hl_tags = _extract_tags(hl)
            if not text:
                continue
            lines.append(f"\n> {text}")
            if note:
                lines.append(f"\n  Note: {note}")
            if hl_tags:
                lines.append(f"  Tags: {', '.join(hl_tags)}")

    content = "\n".join(lines)

    return {
        "id": book_id,
        "title": title,
        "author": author,
        "category": category,
        "source_url": source_url,
        "cover_image_url": cover_image,
        "content": content,
        "_deleted": False,
    }


def _extract_tags(obj: dict) -> list[str]:
    """Extract tag names from a Readwise book or highlight object."""
    tags = obj.get("tags") or []
    if isinstance(tags, list):
        return [t.get("name", "") if isinstance(t, dict) else str(t) for t in tags if t]
    return []


# ──────────────────────────────────────────────────────────────────────────────
# HTTP client
# ──────────────────────────────────────────────────────────────────────────────


class _ReadwiseClient:
    """Thin httpx-based client for the Readwise v2 REST API.

    Handles:
    * Token authentication (Authorization: Token XXX)
    * Retry-After on 429 rate-limit responses
    * Bounded exponential backoff for 5xx / network failures
    """

    def __init__(self, token: str):
        try:
            import httpx
        except ImportError as exc:
            raise ImportError(_EXTRA_HINT) from exc

        self._session = httpx.Client(
            base_url=_API_BASE,
            headers={"Authorization": f"Token {token}"},
            follow_redirects=True,
            timeout=30.0,
        )

    def get(self, path: str, params: dict | None = None) -> dict:
        """GET the given API path with retry logic."""
        for attempt in range(_MAX_RETRIES):
            try:
                # path may be a full URL (pagination next links)
                if path.startswith("http"):
                    resp = self._session.get(path)
                else:
                    resp = self._session.get(path, params=params)
            except Exception as exc:
                if attempt == _MAX_RETRIES - 1:
                    raise
                delay = 2.0 ** attempt
                logger.warning("Readwise: network error, retrying in %.1fs: %s", delay, exc)
                time.sleep(delay)
                continue

            if resp.status_code == 200:
                return resp.json()

            if resp.status_code == 429:
                retry_after = _parse_retry_after(resp)
                if attempt == _MAX_RETRIES - 1:
                    resp.raise_for_status()
                logger.warning(
                    "Readwise: rate limited — retrying in %.1fs (%d/%d).",
                    retry_after, attempt + 1, _MAX_RETRIES,
                )
                time.sleep(retry_after)
                continue

            if resp.status_code in (500, 502, 503, 504):
                if attempt == _MAX_RETRIES - 1:
                    resp.raise_for_status()
                delay = 2.0 ** attempt
                logger.warning(
                    "Readwise: server error %s — retrying in %.1fs.", resp.status_code, delay
                )
                time.sleep(delay)
                continue

            resp.raise_for_status()

        raise RuntimeError(f"Readwise GET {path} failed after {_MAX_RETRIES} attempts.")

    def validate_token(self) -> bool:
        """Return True if the token is valid (GET /auth/ returns 204)."""
        try:
            import httpx

            resp = self._session.get("https://readwise.io/api/v2/auth/")
            return resp.status_code == 204
        except Exception:
            return False


# ──────────────────────────────────────────────────────────────────────────────
# Utilities
# ──────────────────────────────────────────────────────────────────────────────


def _parse_retry_after(resp) -> float:
    """Return seconds to wait from the Retry-After header, or a default."""
    header = resp.headers.get("Retry-After")
    try:
        return max(float(header), 1.0)
    except (TypeError, ValueError):
        return 60.0  # Readwise default rate-limit window


def _utcnow_iso() -> str:
    """Current UTC time as an ISO-8601 string (used as the new cursor)."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
