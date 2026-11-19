"""DLT source for Raindrop.io bookmarks (full-snapshot sync + forget-on-delete).

Fetches bookmarks (title, URL, excerpt, notes, tags) from Raindrop.io, yielding
them as a dlt resource for cognee's ingestion pipeline.

Like the Notion connector, Raindrop.io bookmarks are ingested as *normal
documents*: the source declares ``cognee_document_source = "raindrop"``, so
``resolve_dlt_sources`` tags each row ``external_metadata["source"] = "raindrop"``
and routes through the standard cognify entity-extraction pipeline.

The source is a full snapshot: ``write_disposition="replace"`` rewrites staging
with exactly the bookmarks currently visible to the integration each run.
Deleted bookmarks simply drop out of Raindrop.io's listings and cognee's
``orphan_cleanup`` removes them from the graph and vector stores.

Raindrop.io API docs: https://developer.raindrop.io/
"""

import os
import time
from typing import Any

from cognee.shared.logging_utils import get_logger
from cognee.tasks.ingestion.dlt_utils import DOCUMENT_SOURCE_ATTR

logger = get_logger("raindrop_connector")

RAINDROP_TABLE_NAME = "raindrop_bookmarks"
RAINDROP_SOURCE_NAME = "raindrop"
_API_ROOT = "https://api.raindrop.io"
_API_PREFIX = "/rest/v1"

_MAX_RETRIES = 5

_EXTRA_HINT = (
    "The Raindrop.io connector requires the cognee-community-connector-raindrop package: "
    "pip install cognee-community-connector-raindrop"
)


def raindrop_source(
    token: str | None = None,
    collection_ids: list[int] | None = None,
    client: Any = None,
):
    """Create a dlt source that yields Raindrop.io bookmarks as documents.

    Args:
        token: Raindrop.io test token (https://app.raindrop.io/#settings/apps).
            Falls back to ``RAINDROP_TOKEN`` env var.
        collection_ids: Restrict ingestion to these collection ids. When omitted,
        all collections the token can see are fetched.
        client: Pre-built httpx client (test-injection point); when omitted one
        is built from the token.

    Returns:
        A dlt source suitable for ``cognee.add(...)`` / ``cognee.remember(...)``.
    """
    try:
        import dlt
    except ImportError as exc:
        raise ImportError(_EXTRA_HINT) from exc

    if client is None:
        resolved_token = token or os.environ.get("RAINDROP_TOKEN")
        if not resolved_token:
            raise ValueError(
                "Raindrop.io token required: pass token= or set RAINDROP_TOKEN."
            )
        import httpx

        client = httpx.Client(
            base_url=_API_ROOT,
            headers={"Authorization": f"Bearer {resolved_token}"},
            timeout=30.0,
        )

    @dlt.resource(name=RAINDROP_TABLE_NAME, primary_key="id", write_disposition="replace")
    def bookmarks():
        count = 0
        for bookmark in _iter_bookmarks(client, collection_ids):
            count += 1
            yield _bookmark_to_row(bookmark)
        logger.info("Raindrop.io: synced %d bookmark(s).", count)

    @dlt.source(name=RAINDROP_SOURCE_NAME)
    def _raindrop():
        return bookmarks

    source = _raindrop()
    setattr(source, DOCUMENT_SOURCE_ATTR, RAINDROP_SOURCE_NAME)
    return source


# ---------------------------------------------------------------------------
# Raindrop.io API helpers
# ---------------------------------------------------------------------------


def _request(client, method: str, path: str, **kwargs) -> dict:
    """Call a Raindrop.io API endpoint, retrying transient errors."""
    for attempt in range(_MAX_RETRIES):
        try:
            response = getattr(client, method)(path, **kwargs)
            response.raise_for_status()
            return response.json()
        except Exception as exc:
            if attempt == _MAX_RETRIES - 1 or not _is_transient(exc):
                raise
            delay = 2**attempt
            logger.warning(
                "Raindrop.io: %s — retrying in %.1fs (%d/%d).",
                exc, delay, attempt + 1, _MAX_RETRIES,
            )
            time.sleep(delay)


def _is_transient(exc: Exception) -> bool:
    """True for rate-limit / server / timeout / network errors worth retrying."""
    import httpx

    if isinstance(exc, httpx.TransportError):
        return True
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code in (429, 500, 502, 503, 504)
    return False


def _paginate(client, method: str, path: str, **kwargs) -> list[dict]:
    """Paginate through a Raindrop.io endpoint.

    Raindrop.io uses page-based pagination: page increments from 0, and the
    loop stops when the returned items list is empty or shorter than per_page.
    """
    page = 0
    per_page = 50  # max allowed by Raindrop.io API
    all_items = []
    while True:
        data = _request(client, method, path, params={**kwargs, "page": page, "perpage": per_page})
        items = data.get("items", [])
        all_items.extend(items)
        if len(items) < per_page:
            break
        page += 1
    return all_items


def _iter_bookmarks(client, collection_ids: list[int] | None = None):
    """Yield bookmark dicts from Raindrop.io.

    When collection_ids is None, uses the /raindrops/0 all-bookmarks endpoint
    which returns every bookmark across all collections (including nested and
    system collections like Unsorted). When collection_ids is provided, only
    bookmarks from those specific collections are fetched.
    """
    if collection_ids is None:
        # /raindrops/0 returns all bookmarks regardless of collection
        bookmarks = _paginate(client, "get", f"{_API_PREFIX}/raindrops/0")
        yield from bookmarks
    else:
        for cid in collection_ids:
            bookmarks = _paginate(client, "get", f"{_API_PREFIX}/raindrops/{cid}")
            yield from bookmarks


def _bookmark_to_row(bookmark: dict) -> dict:
    """Flatten a Raindrop.io bookmark into a document row.

    Only ``id``, ``title``, ``content`` (notes), ``excerpt``, ``url``,
    and ``tags`` are kept for the document. A metadata-only edit that
    changes only ``lastUpdate`` without changing text does not churn the
    content-hash data_id.
    """
    return {
        "id": str(bookmark.get("id", "")),
        "url": bookmark.get("link", ""),
        "title": bookmark.get("title", ""),
        "content": _build_content(bookmark),
    }


def _build_content(bookmark: dict) -> str:
    """Build document content from bookmark fields.

    Combines title, excerpt, notes, and tags into a single text block.
    """
    parts = []

    title = bookmark.get("title", "")
    if title:
        parts.append(f"# {title}")

    excerpt = bookmark.get("excerpt", "")
    if excerpt:
        parts.append(excerpt)

    note = bookmark.get("note", "")
    if note:
        parts.append(f"**Notes:** {note}")

    tags = bookmark.get("tags", [])
    if tags:
        parts.append(f"**Tags:** {', '.join(tags)}")

    return "\n\n".join(parts)
