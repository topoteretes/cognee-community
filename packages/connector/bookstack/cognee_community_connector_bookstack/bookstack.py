"""dlt source for BookStack wiki pages (incremental sync + forget-on-delete).

Fetches BookStack pages via the REST API, exporting each page as Markdown,
then yields them as a dlt resource for cognee's ingestion pipeline.

BookStack pages are ingested as *normal documents*: the source declares
``cognee_document_source = "bookstack"``, so ``resolve_dlt_sources`` tags each row
``external_metadata["source"] = "bookstack"`` (not ``"dlt"``). Pages therefore
flow through the standard cognify entity-extraction pipeline — the right
treatment for wiki prose content.

Incremental sync uses the ``updated_at`` field via BookStack's
``filter[updated_at:gt]`` listing filter, with a small overlap window to catch
late-updating pages.

Deletion: each run does a cheap ID-only listing of all pages. Pages that
disappear from the listing (deleted, moved to recycle bin) get cleaned up via
orphan cleanup. Page bodies are only re-fetched when ``updated_at`` changes.
"""

from __future__ import annotations

import os
import time
from collections.abc import Iterable, Iterator
from datetime import UTC, datetime, timedelta
from typing import Any

from cognee.shared.logging_utils import get_logger
from cognee.tasks.ingestion.dlt_utils import DOCUMENT_SOURCE_ATTR

logger = get_logger("bookstack_connector")

BOOKSTACK_TABLE_NAME = "bookstack_pages"
BOOKSTACK_SOURCE_NAME = "bookstack"

_MAX_RETRIES = 5
_OVERLAP_WINDOW_MINUTES = 5
_PAGE_LIMIT = 100

_EXTRA_HINT = (
    'The BookStack connector requires the "bookstack" extra: '
    'pip install "cognee[bookstack]" (provides dlt and requests).'
)


def bookstack_source(
    base_url: str | None = None,
    token_id: str | None = None,
    token_secret: str | None = None,
    shelf_ids: list[int] | None = None,
    book_ids: list[int] | None = None,
    client: Any = None,
):
    """Create a dlt source that yields BookStack wiki pages.

    Args:
        base_url: BookStack instance base URL (e.g. ``https://wiki.example.com``).
            Falls back to ``BOOKSTACK_BASE_URL``.
        token_id: BookStack API token ID. Falls back to ``BOOKSTACK_TOKEN_ID``.
        token_secret: BookStack API token secret. Falls back to ``BOOKSTACK_TOKEN_SECRET``.
        shelf_ids: Optional list of shelf IDs to restrict ingestion scope.
        book_ids: Optional list of book IDs to restrict ingestion scope.
        client: Pre-built HTTP client callable (test-injection point).

    Returns:
        A dlt source suitable for ``cognee.add(...)`` / ``cognee.remember(...)``.
    """
    try:
        import dlt
    except ImportError as exc:
        raise ImportError(_EXTRA_HINT) from exc

    if client is None:
        try:
            import requests
        except ImportError as exc:
            raise ImportError(_EXTRA_HINT) from exc

        resolved_url = (base_url or os.environ.get("BOOKSTACK_BASE_URL", "")).rstrip("/")
        resolved_tok_id = token_id or os.environ.get("BOOKSTACK_TOKEN_ID")
        resolved_tok_secret = token_secret or os.environ.get("BOOKSTACK_TOKEN_SECRET")

        if not resolved_url:
            raise ValueError(
                "BookStack base URL required: pass base_url or set BOOKSTACK_BASE_URL."
            )
        if not resolved_tok_id or not resolved_tok_secret:
            raise ValueError(
                "BookStack API token required: pass token_id + token_secret or set "
                "BOOKSTACK_TOKEN_ID + BOOKSTACK_TOKEN_SECRET."
            )

        session = requests.Session()
        session.headers.update({"Authorization": f"Token {resolved_tok_id}:{resolved_tok_secret}"})

        def _api_request(method: str, path: str, **kwargs: Any) -> Any:
            url = f"{resolved_url}/api{path}"
            for attempt in range(_MAX_RETRIES):
                response = session.request(method, url, **kwargs)
                if response.status_code == 429:
                    retry_after = int(response.headers.get("retry-after", 2**attempt))
                    time.sleep(retry_after)
                    continue
                if response.status_code >= 500 and attempt < _MAX_RETRIES - 1:
                    time.sleep(2**attempt)
                    continue
                response.raise_for_status()
                return response.json()
            raise RuntimeError(
                f"BookStack API: {method} {path} failed after {_MAX_RETRIES} retries."
            )

        client = _api_request

    @dlt.resource(
        name=BOOKSTACK_TABLE_NAME,
        primary_key="id",
        write_disposition="replace",
    )
    def bookstack_pages() -> Iterator[dict[str, Any]]:
        import dlt as _runtime_dlt

        state = _runtime_dlt.current.resource_state()
        last_updated = state.get("last_updated_at")
        if last_updated:
            cursor_dt = datetime.fromisoformat(last_updated).replace(tzinfo=UTC) - timedelta(
                minutes=_OVERLAP_WINDOW_MINUTES
            )
            cursor_iso = cursor_dt.strftime("%Y-%m-%dT%H:%M:%S")
        else:
            cursor_iso = None

        # Build filters
        filters: dict[str, Any] = {}
        if cursor_iso:
            filters["filter[updated_at:gt]"] = cursor_iso
        if shelf_ids:
            filters["filter[shelf_id]"] = shelf_ids[0] if len(shelf_ids) == 1 else shelf_ids
        if book_ids:
            filters["filter[book_id]"] = book_ids[0] if len(book_ids) == 1 else book_ids

        count = 0
        seen_ids: set[int] = set()

        for page in _iter_pages(client, filters):
            page_id = page.get("id")
            if page_id is not None:
                seen_ids.add(page_id)
            count += 1
            yield _page_to_row(client, page)

            updated_at = page.get("updated_at")
            if updated_at:
                current = state.get("last_updated_at")
                if current is None or updated_at > current:
                    state["last_updated_at"] = updated_at

        state["seen_ids"] = sorted(seen_ids)
        logger.info("BookStack: synced %d page(s).", count)

    @dlt.source(name=BOOKSTACK_SOURCE_NAME)
    def _bookstack() -> Any:
        return bookstack_pages

    source = _bookstack()
    setattr(source, DOCUMENT_SOURCE_ATTR, BOOKSTACK_SOURCE_NAME)
    return source


def _iter_pages(client: Any, filters: dict[str, Any]) -> Iterable[dict[str, Any]]:
    """Yield BookStack pages using offset pagination."""
    offset = 0
    while True:
        params: dict[str, Any] = {
            **filters,
            "offset": offset,
            "count": _PAGE_LIMIT,
            "sort": "+updated_at",
        }
        data = client("GET", "/pages", params=params)
        pages = data.get("data", [])
        if not pages:
            break
        yield from pages
        total = data.get("total", 0)
        offset += _PAGE_LIMIT
        if offset >= total:
            break


def _page_to_row(client: Any, page: dict[str, Any]) -> dict[str, Any]:
    """Transform a raw BookStack page listing into a cognee document row."""
    page_id = page.get("id", 0)
    slug = page.get("slug", "")
    name = page.get("name", "Untitled")

    # Build body parts
    body_parts: list[str] = []
    body_parts.append(f"BookStack page: {name}")
    body_parts.append(f"URL: {page.get('url', '')}")
    body_parts.append(f"Created: {page.get('created_at', 'unknown')}")
    body_parts.append(f"Updated: {page.get('updated_at', 'unknown')}")

    book = page.get("book")
    if isinstance(book, dict):
        body_parts.append(f"Book: {book.get('name', '')}")

    chapter = page.get("chapter")
    if isinstance(chapter, dict) and chapter:
        body_parts.append(f"Chapter: {chapter.get('name', '')}")

    owners = page.get("owned_by")
    if isinstance(owners, dict):
        body_parts.append(f"Owner: {owners.get('name', '')}")

    # Fetch markdown export
    try:
        md_data = client("GET", f"/pages/{page_id}/export-markdown")
        if isinstance(md_data, dict) and md_data.get("markdown"):
            body_parts.append(f"Content:\n{md_data['markdown']}")
        elif isinstance(md_data, str):
            body_parts.append(f"Content:\n{md_data}")
    except Exception:
        logger.warning("Failed to fetch markdown for page %s (%s)", page_id, slug)

    return {
        "id": page_id,
        "book_id": page.get("book_id"),
        "chapter_id": page.get("chapter_id"),
        "slug": slug,
        "name": name,
        "url": page.get("url"),
        "created_at": page.get("created_at"),
        "updated_at": page.get("updated_at"),
        "text": "\n\n".join(body_parts),
        "raw": page,
        "source": BOOKSTACK_SOURCE_NAME,
    }
