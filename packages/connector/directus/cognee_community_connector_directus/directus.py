"""DLT source for Directus collections (full-snapshot sync + forget-on-delete).

Fetches Directus collection items across configured collections and yields them as a dlt
resource for cognee's ingestion pipeline.

Like other document connectors, Directus items are ingested as documents:
the source declares ``cognee_document_source = "directus"``, routing records through
cognee's cognify entity-extraction pipeline.

The source uses full-snapshot synchronization (``write_disposition="replace"``).
Deletions in Directus propagate automatically: deleted items drop out of subsequent listings
and cognee's ``orphan_cleanup`` purges them from the knowledge graph and vector indexes.
Unchanged items maintain a deterministic content-hash and avoid unnecessary re-computation.
"""

import os
import time
from collections.abc import Iterator
from typing import Any

from cognee.shared.logging_utils import get_logger
from cognee.tasks.ingestion.dlt_utils import DOCUMENT_SOURCE_ATTR

logger = get_logger("directus_connector")

DIRECTUS_TABLE_NAME = "directus_records"
DIRECTUS_SOURCE_NAME = "directus"

_MAX_RETRIES = 5
_PER_PAGE = 50

# Internal or sensitive Directus fields that should not be indexed in document content.
_DEFAULT_IGNORED_FIELDS = {
    "password",
    "token",
    "auth_data",
    "tfa_secret",
    "salt",
    "refresh_token",
    "secret",
}


class DirectusClient:
    """Lightweight HTTP client for Directus REST API."""

    def __init__(self, base_url: str, auth_token: str | None = None, http_client: Any = None):
        import httpx

        self.base_url = base_url.rstrip("/")
        self.auth_token = auth_token
        self.client = http_client or httpx.Client(timeout=30.0)
        self._owned_client = http_client is None

    def _headers(self) -> dict[str, str]:
        headers = {"Accept": "application/json"}
        if self.auth_token:
            headers["Authorization"] = f"Bearer {self.auth_token}"
        return headers

    def close(self) -> None:
        """Close underlying HTTP client connections."""
        if self._owned_client and hasattr(self.client, "close"):
            self.client.close()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()

    def request(self, method: str, endpoint: str, **kwargs) -> dict[str, Any]:
        """Execute request with retries on rate-limit or transient server errors."""
        url = f"{self.base_url}{endpoint}"
        headers = {**self._headers(), **kwargs.pop("headers", {})}

        for attempt in range(_MAX_RETRIES):
            try:
                response = self.client.request(method, url, headers=headers, **kwargs)
                response.raise_for_status()
                return response.json()
            except Exception as exc:
                if attempt == _MAX_RETRIES - 1 or not self._is_transient(exc):
                    raise
                delay = self._retry_delay(exc, attempt)
                logger.warning(
                    "Directus: %s — retrying in %.1fs (%d/%d).",
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

    def get_collections(self) -> list[str]:
        """Fetch all user collections from Directus (excluding system collections)."""
        data = self.request("GET", "/collections")
        items = data.get("data", [])
        return [
            c.get("collection")
            for c in items
            if isinstance(c, dict)
            and c.get("collection")
            and not c.get("collection", "").startswith("directus_")
        ]

    def iter_items(
        self,
        collection: str,
        filter_query: dict[str, Any] | None = None,
        sort: str | None = "-date_updated",
        fields: str = "*",
    ) -> Iterator[dict[str, Any]]:
        """Stream items in a collection across pages as a memory-efficient generator."""
        offset = 0

        while True:
            params: dict[str, Any] = {
                "limit": _PER_PAGE,
                "offset": offset,
                "fields": fields,
            }
            if sort:
                params["sort"] = sort
            if filter_query:
                import json

                params["filter"] = json.dumps(filter_query)

            data = self.request(
                "GET",
                f"/items/{collection}",
                params=params,
            )
            items = data.get("data", [])
            if not items:
                break

            yield from items

            if len(items) < _PER_PAGE:
                break
            offset += _PER_PAGE


def directus_source(
    base_url: str | None = None,
    auth_token: str | None = None,
    collections: list[str] | None = None,
    filter_query: dict[str, Any] | None = None,
    sort: str | None = "-date_updated",
    fields: str = "*",
    ignored_fields: set[str] | None = None,
    client: Any = None,
):
    """Create a dlt source that yields Directus items as document items.

    Args:
        base_url: Base URL of Directus instance (e.g. ``http://127.0.0.1:8055``).
            Falls back to ``DIRECTUS_URL``.
        auth_token: Directus static API token or user token. Falls back to
            ``DIRECTUS_TOKEN``. Optional if collections permit public read.
        collections: List of collection names to sync. If omitted, user
            collections accessible to the token are auto-discovered.
        filter_query: Optional Directus filter dictionary
            (e.g. ``{"status": {"_eq": "published"}}``).
        sort: Optional sort expression (e.g. ``"-date_updated"``). Set to ``None`` for
            collections without timestamp columns.
        fields: Comma-separated list of fields or ``"*"`` (default).
        ignored_fields: Optional additional fields to exclude from document content.
        client: Pre-built ``DirectusClient`` (primarily for test mocking).

    Returns:
        A dlt source ready for ``cognee.add(...)`` or ``cognee.remember(...)``.
    """
    try:
        import dlt
    except ImportError as exc:
        raise ImportError(
            'The Directus connector requires dlt: run "pip install dlt[sqlalchemy]"'
        ) from exc

    resolved_base_url = (
        base_url or os.environ.get("DIRECTUS_URL") or "http://127.0.0.1:8055"
    )
    resolved_token = auth_token or os.environ.get("DIRECTUS_TOKEN")

    effective_ignored = set(_DEFAULT_IGNORED_FIELDS)
    if ignored_fields:
        effective_ignored.update(ignored_fields)

    dir_client = client or DirectusClient(
        base_url=resolved_base_url,
        auth_token=resolved_token,
    )

    @dlt.resource(name=DIRECTUS_TABLE_NAME, primary_key="id", write_disposition="replace")
    def directus_records():
        try:
            active_collections = collections
            if not active_collections:
                try:
                    active_collections = dir_client.get_collections()
                except Exception as exc:
                    logger.warning(
                        "Directus: unable to list collections (%s). Specify collections=[...].",
                        exc,
                    )
                    active_collections = []

            count = 0
            for collection_name in active_collections:
                try:
                    items_iter = dir_client.iter_items(
                        collection_name,
                        filter_query=filter_query,
                        sort=sort,
                        fields=fields,
                    )
                except Exception as exc:
                    logger.error(
                        "Directus: failed to fetch items for collection %s: %s",
                        collection_name,
                        exc,
                    )
                    raise

                for item in items_iter:
                    row = _item_to_row(
                        dir_client.base_url,
                        collection_name,
                        item,
                        ignored=effective_ignored,
                    )
                    if row:
                        count += 1
                        yield row

            logger.info("Directus: synced %d item(s).", count)
        finally:
            if client is None:
                dir_client.close()

    @dlt.source(name=DIRECTUS_SOURCE_NAME)
    def _directus():
        return directus_records

    source = _directus()
    setattr(source, DOCUMENT_SOURCE_ATTR, DIRECTUS_SOURCE_NAME)
    return source


def _item_to_row(
    base_url: str,
    collection: str,
    item: dict[str, Any],
    ignored: set[str] | None = None,
) -> dict[str, Any]:
    """Transform a Directus item into a standardized document row."""
    item_id = item.get("id", "")
    if item_id is None or item_id == "":
        return {}

    title = _extract_title(item, collection)
    content = _render_item_content(item, ignored=ignored)

    return {
        "id": f"{collection}:{item_id}",
        "url": f"{base_url}/items/{collection}/{item_id}",
        "title": title,
        "content": content,
    }


def _extract_title(item: dict[str, Any], collection: str) -> str:
    """Extract a human-readable title from common headline fields."""
    for candidate in ("title", "name", "subject", "headline", "label", "slug"):
        val = item.get(candidate)
        if isinstance(val, str) and val.strip():
            return val.strip()
    return f"{collection} {item.get('id', '')}"


def _render_item_content(
    item: dict[str, Any],
    ignored: set[str] | None = None,
) -> str:
    """Format item fields into readable document text for cognify."""
    lines: list[str] = []
    ignored_keys = ignored if ignored is not None else _DEFAULT_IGNORED_FIELDS

    body_fields = ("body", "content", "description", "text", "notes", "summary", "article")
    primary_text = ""
    for field in body_fields:
        if field in item and isinstance(item[field], str) and item[field].strip():
            primary_text = item[field].strip()
            lines.append(primary_text)
            lines.append("")
            break

    metadata_lines: list[str] = []
    for key, value in sorted(item.items()):
        if key in ignored_keys or key in body_fields:
            continue
        if value is None or value == "":
            continue
        if isinstance(value, (list, dict)):
            import json

            val_str = json.dumps(value, ensure_ascii=False)
        else:
            val_str = str(value)
        metadata_lines.append(f"{key}: {val_str}")

    if metadata_lines:
        if lines:
            lines.append("---")
        lines.extend(metadata_lines)

    return "\n".join(lines).strip()
