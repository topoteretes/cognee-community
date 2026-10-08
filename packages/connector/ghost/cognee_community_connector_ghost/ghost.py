"""DLT source for Ghost CMS publications (full-snapshot sync + forget-on-delete).

Fetches posts and pages from Ghost's Content API and yields them as a dlt resource
for cognee's ingestion pipeline.

Editorial articles and pages are ingested as documents: the source declares
``cognee_document_source = "ghost"``, routing records through cognee's cognify
entity-extraction pipeline.

The source uses full-snapshot synchronization (``write_disposition="replace"``).
Deletions in Ghost propagate automatically: unpublished or deleted posts drop out of
subsequent listings and cognee's ``orphan_cleanup`` purges them from the knowledge
graph and vector indexes. Unchanged posts maintain a deterministic content-hash
and avoid unnecessary re-computation.
"""

import os
import time
from collections.abc import Iterator
from typing import Any

from cognee.shared.logging_utils import get_logger
from cognee.tasks.ingestion.dlt_utils import DOCUMENT_SOURCE_ATTR

logger = get_logger("ghost_connector")

GHOST_TABLE_NAME = "ghost_documents"
GHOST_SOURCE_NAME = "ghost"

_MAX_RETRIES = 5
_PER_PAGE = 50


class GhostClient:
    """Lightweight HTTP client for Ghost Content API v5."""

    def __init__(
        self,
        base_url: str,
        content_api_key: str | None = None,
        http_client: Any = None,
    ):
        import httpx

        self.base_url = base_url.rstrip("/")
        self.content_api_key = content_api_key
        self.client = http_client or httpx.Client(timeout=30.0)
        self._owned_client = http_client is None

    def close(self) -> None:
        """Close underlying HTTP client connections."""
        if self._owned_client and hasattr(self.client, "close"):
            self.client.close()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()

    def request(self, endpoint: str, **kwargs) -> dict[str, Any]:
        """Execute request with retries on rate-limit or transient server errors."""
        url = f"{self.base_url}{endpoint}"
        params = kwargs.pop("params", {}) or {}
        if self.content_api_key:
            params["key"] = self.content_api_key

        for attempt in range(_MAX_RETRIES):
            try:
                response = self.client.get(url, params=params, **kwargs)
                response.raise_for_status()
                return response.json()
            except Exception as exc:
                if attempt == _MAX_RETRIES - 1 or not self._is_transient(exc):
                    raise
                delay = self._retry_delay(exc, attempt)
                logger.warning(
                    "Ghost: %s — retrying in %.1fs (%d/%d).",
                    exc,
                    delay,
                    attempt + 1,
                    _MAX_RETRIES,
                )
                time.sleep(delay)
        return {}

    @staticmethod
    def _is_transient(exc: Exception) -> bool:
        import httpx

        if isinstance(exc, (httpx.TimeoutException, httpx.NetworkError)):
            return True
        if isinstance(exc, httpx.HTTPStatusError):
            return exc.response.status_code in (429, 500, 502, 503, 504)
        return False

    @staticmethod
    def _retry_delay(exc: Exception, attempt: int) -> float:
        import httpx

        if isinstance(exc, httpx.HTTPStatusError):
            retry_header = exc.response.headers.get("Retry-After")
            if retry_header:
                try:
                    return float(retry_header)
                except (ValueError, TypeError):
                    pass
        return float(2**attempt)

    def iter_documents(
        self,
        resource_type: str = "posts",
        filter_query: str | None = None,
    ) -> Iterator[dict[str, Any]]:
        """Stream posts or pages across pages as a memory-efficient generator."""
        page = 1

        while True:
            params: dict[str, Any] = {
                "page": page,
                "limit": _PER_PAGE,
                "include": "tags,authors",
                "formats": "plaintext,html",
            }
            if filter_query:
                params["filter"] = filter_query

            data = self.request(f"/ghost/api/content/{resource_type}/", params=params)
            items = data.get(resource_type, [])
            if not items:
                break

            yield from items

            pagination = data.get("meta", {}).get("pagination", {})
            total_pages = pagination.get("pages", 1)
            if page >= total_pages:
                break
            page += 1


def ghost_source(
    base_url: str | None = None,
    content_api_key: str | None = None,
    include_posts: bool = True,
    include_pages: bool = True,
    filter_query: str | None = None,
    client: Any = None,
):
    """Create a dlt source that yields Ghost posts and pages as document items.

    Args:
        base_url: Base URL of Ghost publication (e.g. ``https://demo.ghost.io``).
            Falls back to ``GHOST_URL``.
        content_api_key: Ghost Content API key. Falls back to ``GHOST_CONTENT_API_KEY``.
        include_posts: Ingest published posts (default: True).
        include_pages: Ingest published static pages (default: True).
        filter_query: Optional NQL filter (e.g. ``tag:engineering+featured:true``).
        client: Pre-built ``GhostClient`` (primarily for test mocking).

    Returns:
        A dlt source ready for ``cognee.add(...)`` or ``cognee.remember(...)``.
    """
    try:
        import dlt
    except ImportError as exc:
        raise ImportError(
            'The Ghost connector requires dlt: run "pip install dlt[sqlalchemy]"'
        ) from exc

    resolved_base_url = (
        base_url or os.environ.get("GHOST_URL") or "https://demo.ghost.io"
    )
    resolved_key = content_api_key or os.environ.get("GHOST_CONTENT_API_KEY")

    if not resolved_key and client is None:
        raise ValueError(
            "Ghost Content API key required: pass content_api_key= or set GHOST_CONTENT_API_KEY."
        )

    ghost_client = client or GhostClient(
        base_url=resolved_base_url,
        content_api_key=resolved_key,
    )

    @dlt.resource(name=GHOST_TABLE_NAME, primary_key="id", write_disposition="replace")
    def ghost_documents():
        try:
            count = 0
            resources_to_fetch = []
            if include_posts:
                resources_to_fetch.append("posts")
            if include_pages:
                resources_to_fetch.append("pages")

            for res_type in resources_to_fetch:
                try:
                    items_iter = ghost_client.iter_documents(
                        resource_type=res_type,
                        filter_query=filter_query,
                    )
                except Exception as exc:
                    logger.error(
                        "Ghost: failed to fetch %s: %s",
                        res_type,
                        exc,
                    )
                    raise

                for item in items_iter:
                    row = _document_to_row(ghost_client.base_url, res_type, item)
                    if row:
                        count += 1
                        yield row

            logger.info("Ghost: synced %d document(s).", count)
        finally:
            if client is None:
                ghost_client.close()

    @dlt.source(name=GHOST_SOURCE_NAME)
    def _ghost():
        return ghost_documents

    source = _ghost()
    setattr(source, DOCUMENT_SOURCE_ATTR, GHOST_SOURCE_NAME)
    return source


def _document_to_row(base_url: str, resource_type: str, item: dict[str, Any]) -> dict[str, Any]:
    """Transform a Ghost post or page into a standardized document row."""
    item_id = item.get("id", "")
    if not item_id:
        return {}

    title = item.get("title", f"Untitled {resource_type.rstrip('s')}")
    url = item.get("url") or f"{base_url}/{item.get('slug', item_id)}"
    content = _render_document_content(item)

    singular = resource_type.rstrip("s")
    return {
        "id": f"ghost:{singular}:{item_id}",
        "url": url,
        "title": title,
        "content": content,
    }


def _render_document_content(item: dict[str, Any]) -> str:
    """Format Ghost post/page content with author and tags for cognify."""
    lines: list[str] = []

    # Primary body text
    body = item.get("plaintext") or item.get("html") or item.get("custom_excerpt") or ""
    if body.strip():
        lines.append(body.strip())
        lines.append("")

    metadata_lines: list[str] = []

    # Extract primary authors
    authors = item.get("authors") or []
    if authors and isinstance(authors, list):
        author_names = [a.get("name") for a in authors if isinstance(a, dict) and a.get("name")]
        if author_names:
            metadata_lines.append(f"authors: {', '.join(author_names)}")
    elif item.get("primary_author"):
        pa = item.get("primary_author")
        if isinstance(pa, dict) and pa.get("name"):
            metadata_lines.append(f"author: {pa.get('name')}")

    # Extract tags
    tags = item.get("tags") or []
    if tags and isinstance(tags, list):
        tag_names = [t.get("name") for t in tags if isinstance(t, dict) and t.get("name")]
        if tag_names:
            metadata_lines.append(f"tags: {', '.join(tag_names)}")

    for field in ("published_at", "updated_at", "excerpt"):
        val = item.get(field)
        if val and isinstance(val, str) and val.strip():
            metadata_lines.append(f"{field}: {val.strip()}")

    if metadata_lines:
        if lines:
            lines.append("---")
        lines.extend(metadata_lines)

    return "\n".join(lines).strip()
