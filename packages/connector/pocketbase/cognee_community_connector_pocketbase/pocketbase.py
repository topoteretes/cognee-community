"""DLT source for PocketBase collections (full-snapshot sync + forget-on-delete).

Fetches PocketBase records across configured collections and yields them as a dlt
resource for cognee's ingestion pipeline.

Like the Notion and Confluence connectors, PocketBase records are ingested as
documents: the source declares ``cognee_document_source = "pocketbase"``, routing
records through cognee's cognify entity-extraction pipeline.

The source uses full-snapshot synchronization (``write_disposition="replace"``).
Deletions in PocketBase propagate automatically: deleted records drop out of
subsequent listings and cognee's ``orphan_cleanup`` purges them from the knowledge
graph and vector indexes. Unchanged records maintain a deterministic content-hash
and avoid unnecessary re-computation.
"""

import os
import time
from typing import Any

from cognee.shared.logging_utils import get_logger
from cognee.tasks.ingestion.dlt_utils import DOCUMENT_SOURCE_ATTR

logger = get_logger("pocketbase_connector")

POCKETBASE_TABLE_NAME = "pocketbase_records"
POCKETBASE_SOURCE_NAME = "pocketbase"

_MAX_RETRIES = 5
_PER_PAGE = 50

# Internal or sensitive PocketBase fields that should not be indexed in document content.
_IGNORED_FIELDS = {
    "password",
    "passwordConfirm",
    "tokenKey",
    "emailVisibility",
    "verified",
    "expand",
    "collectionId",
    "collectionName",
}


class PocketBaseClient:
    """Lightweight HTTP client for PocketBase REST API."""

    def __init__(self, base_url: str, auth_token: str | None = None, http_client: Any = None):
        import httpx

        self.base_url = base_url.rstrip("/")
        self.auth_token = auth_token
        self.client = http_client or httpx.Client(timeout=30.0)

    def _headers(self) -> dict[str, str]:
        headers = {"Accept": "application/json"}
        if self.auth_token:
            headers["Authorization"] = f"Bearer {self.auth_token}"
        return headers

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
                    "PocketBase: %s — retrying in %.1fs (%d/%d).",
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

    def get_collections(self) -> list[dict[str, Any]]:
        """Fetch all user collections from PocketBase."""
        data = self.request("GET", "/api/collections", params={"perPage": 200})
        items = data.get("items", [])
        return items

    def iter_records(
        self, collection: str, filter_query: str | None = None
    ) -> list[dict[str, Any]]:
        """Iterate all records in a collection across pages."""
        page = 1
        records: list[dict[str, Any]] = []

        while True:
            params: dict[str, Any] = {
                "page": page,
                "perPage": _PER_PAGE,
                "sort": "+created",
            }
            if filter_query:
                params["filter"] = filter_query

            data = self.request(
                "GET",
                f"/api/collections/{collection}/records",
                params=params,
            )
            items = data.get("items", [])
            if not items:
                break

            records.extend(items)
            total_pages = data.get("totalPages", 1)
            if page >= total_pages:
                break
            page += 1

        return records


def pocketbase_source(
    base_url: str | None = None,
    auth_token: str | None = None,
    collections: list[str] | None = None,
    client: Any = None,
):
    """Create a dlt source that yields PocketBase records as document items.

    Args:
        base_url: Base URL of PocketBase instance (e.g. ``http://127.0.0.1:8090``).
            Falls back to ``POCKETBASE_URL``.
        auth_token: PocketBase admin or user auth token. Falls back to
            ``POCKETBASE_TOKEN``. Optional if collections permit public read.
        collections: List of collection names to sync. If omitted, all base
            collections accessible to the token are retrieved.
        client: Pre-built ``PocketBaseClient`` (primarily for test mocking).

    Returns:
        A dlt source ready for ``cognee.add(...)`` or ``cognee.remember(...)``.
    """
    try:
        import dlt
    except ImportError as exc:
        raise ImportError(
            'The PocketBase connector requires dlt: run "pip install dlt[sqlalchemy]"'
        ) from exc

    resolved_base_url = (
        base_url or os.environ.get("POCKETBASE_URL") or "http://127.0.0.1:8090"
    )
    resolved_token = auth_token or os.environ.get("POCKETBASE_TOKEN")

    if client is None:
        client = PocketBaseClient(
            base_url=resolved_base_url,
            auth_token=resolved_token,
        )

    @dlt.resource(name=POCKETBASE_TABLE_NAME, primary_key="id", write_disposition="replace")
    def pocketbase_records():
        active_collections = collections
        if not active_collections:
            # Auto-discover collections if none were explicitly specified.
            try:
                colls = client.get_collections()
                # Exclude internal system auth collections (like _superusers)
                active_collections = [
                    c.get("name")
                    for c in colls
                    if c.get("name") and not c.get("name", "").startswith("_")
                ]
            except Exception as exc:
                logger.warning(
                    "PocketBase: unable to list collections (%s). Specify collections=[...].",
                    exc,
                )
                active_collections = []

        count = 0
        for collection_name in active_collections:
            try:
                records = client.iter_records(collection_name)
            except Exception as exc:
                logger.error(
                    "PocketBase: failed to fetch records for collection %s: %s",
                    collection_name,
                    exc,
                )
                raise

            for record in records:
                row = _record_to_row(client.base_url, collection_name, record)
                if row:
                    count += 1
                    yield row

        logger.info("PocketBase: synced %d record(s).", count)

    @dlt.source(name=POCKETBASE_SOURCE_NAME)
    def _pocketbase():
        return pocketbase_records

    source = _pocketbase()
    setattr(source, DOCUMENT_SOURCE_ATTR, POCKETBASE_SOURCE_NAME)
    return source


def _record_to_row(base_url: str, collection: str, record: dict[str, Any]) -> dict[str, Any]:
    """Transform a PocketBase record into a standardized document row."""
    rec_id = record.get("id", "")
    if not rec_id:
        return {}

    title = _extract_title(record, collection)
    content = _render_record_content(record)

    return {
        "id": f"{collection}:{rec_id}",
        "url": f"{base_url}/api/collections/{collection}/records/{rec_id}",
        "title": title,
        "content": content,
    }


def _extract_title(record: dict[str, Any], collection: str) -> str:
    """Extract a human-readable title from common headline fields."""
    for candidate in ("title", "name", "subject", "headline", "label", "slug"):
        val = record.get(candidate)
        if isinstance(val, str) and val.strip():
            return val.strip()
    return f"{collection} {record.get('id', '')}"


def _render_record_content(record: dict[str, Any]) -> str:
    """Format record fields into readable document text for cognify."""
    lines: list[str] = []

    # Prioritize dedicated body / text / description fields at the top
    body_fields = ("body", "content", "description", "text", "notes", "summary")
    primary_text = ""
    for field in body_fields:
        if field in record and isinstance(record[field], str) and record[field].strip():
            primary_text = record[field].strip()
            lines.append(primary_text)
            lines.append("")
            break

    # Format remaining custom attributes
    metadata_lines: list[str] = []
    for key, value in sorted(record.items()):
        if key in _IGNORED_FIELDS or key in body_fields:
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
