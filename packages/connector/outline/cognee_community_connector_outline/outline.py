"""Outline Knowledge Base data-source connector for cognee.

Extracts Outline wiki documents and collections, formats them into markdown documents,
and loads them into Cognee with document-mode routing (cognify entity extraction)
and full snapshot replace semantics (forget-on-delete).
"""

import json
import os
import time
from collections.abc import Iterator
from typing import Any

from cognee.shared.logging_utils import get_logger
from cognee.tasks.ingestion.dlt_utils import DOCUMENT_SOURCE_ATTR

logger = get_logger("outline_connector")

OUTLINE_TABLE_NAME = "outline_documents"
OUTLINE_SOURCE_NAME = "outline"

_MAX_RETRIES = 5
_PER_PAGE = 50
_SENSITIVE_FIELD_NAMES = {
    "token",
    "access_token",
    "secret",
    "password",
    "api_key",
    "apikey",
    "auth_data",
}


class OutlineClient:
    """Lightweight HTTP client for the Outline REST API."""

    def __init__(
        self,
        base_url: str = "https://app.getoutline.com/api",
        api_token: str | None = None,
        http_client: Any = None,
    ):
        import httpx

        self.base_url = base_url.rstrip("/")
        self.api_token = api_token

        headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
        }
        if self.api_token:
            headers["Authorization"] = f"Bearer {self.api_token}"

        self.client = http_client or httpx.Client(
            base_url=self.base_url,
            headers=headers,
            timeout=30.0,
        )
        self._owned_client = http_client is None

    def close(self) -> None:
        """Close underlying HTTP client connections."""
        if self._owned_client and hasattr(self.client, "close"):
            self.client.close()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()

    def post(self, endpoint: str, json_data: dict[str, Any] | None = None) -> dict[str, Any]:
        """Execute POST request with retries on rate limits or transient errors."""
        endpoint = endpoint if endpoint.startswith("/") else f"/{endpoint}"
        payload = json_data or {}

        for attempt in range(_MAX_RETRIES):
            try:
                response = self.client.post(endpoint, json=payload)
                response.raise_for_status()
                return response.json()
            except Exception as exc:
                if attempt == _MAX_RETRIES - 1 or not self._is_transient(exc):
                    raise
                delay = self._retry_delay(exc, attempt)
                err_msg = (
                    f"HTTP {exc.response.status_code}"
                    if hasattr(exc, "response") and exc.response is not None
                    else type(exc).__name__
                )
                logger.warning(
                    "Outline: %s — retrying in %.1fs (%d/%d).",
                    err_msg,
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
                    return min(float(retry_header), 60.0)
                except (ValueError, TypeError):
                    pass
        return min(float(2**attempt), 60.0)

    def iter_collections(self, limit: int = _PER_PAGE) -> Iterator[dict[str, Any]]:
        """List accessible Outline collections."""
        offset = 0

        while True:
            data = self.post("/collections.list", {"offset": offset, "limit": limit})
            collections = data.get("data") or []
            if not collections:
                break

            yield from collections

            pagination = data.get("pagination") or {}
            next_path = pagination.get("nextPath")
            offset += len(collections)
            if not next_path:
                break

    def iter_documents(
        self,
        collection_id: str | None = None,
        limit: int = _PER_PAGE,
    ) -> Iterator[dict[str, Any]]:
        """Paginate and stream Outline documents."""
        offset = 0

        while True:
            payload: dict[str, Any] = {
                "offset": offset,
                "limit": limit,
            }
            if collection_id:
                payload["collectionId"] = collection_id

            data = self.post("/documents.list", payload)
            docs = data.get("data") or []
            if not docs:
                break

            yield from docs

            pagination = data.get("pagination") or {}
            next_path = pagination.get("nextPath")
            offset += len(docs)
            if not next_path:
                break


def outline_source(
    base_url: str | None = None,
    api_token: str | None = None,
    collection_ids: list[str] | None = None,
    client: OutlineClient | None = None,
):
    """Create a dlt source ingesting Outline knowledge base documents into Cognee.

    Args:
        base_url: Outline API base URL (default: ``https://app.getoutline.com/api``).
        api_token: Outline API token. Falls back to ``OUTLINE_API_KEY`` or ``OUTLINE_TOKEN``.
        collection_ids: Optional list of collection IDs to ingest. If None,
            syncs all accessible collections.
        client: Pre-configured ``OutlineClient`` instance (e.g. for testing).

    Returns:
        A dlt source ready for ``cognee.remember(...)`` or ``cognee.add(...)``.
    """
    try:
        import dlt
    except ImportError as exc:
        raise ImportError(
            'The Outline connector requires dlt: run "pip install dlt[sqlalchemy]"'
        ) from exc

    resolved_base_url = (
        base_url
        or os.environ.get("OUTLINE_URL")
        or os.environ.get("OUTLINE_BASE_URL")
        or "https://app.getoutline.com/api"
    )
    resolved_token = (
        api_token
        or os.environ.get("OUTLINE_API_KEY")
        or os.environ.get("OUTLINE_TOKEN")
    )

    if not resolved_token and client is None:
        raise ValueError(
            "Outline API token required: pass api_token= or set OUTLINE_API_KEY / OUTLINE_TOKEN."
        )

    outline_client = client or OutlineClient(
        base_url=resolved_base_url,
        api_token=resolved_token,
    )

    @dlt.resource(
        name=OUTLINE_TABLE_NAME,
        primary_key="id",
        write_disposition="replace",
    )
    def outline_documents():
        try:
            count = 0
            # If specific collections are requested, stream per collection
            if collection_ids:
                for col_id in collection_ids:
                    try:
                        docs_iter = outline_client.iter_documents(collection_id=col_id)
                    except Exception as exc:
                        logger.error(
                            "Outline: failed to fetch documents for collection %s: %s",
                            col_id,
                            exc,
                        )
                        raise

                    for doc in docs_iter:
                        row = _document_to_row(outline_client.base_url, doc)
                        if row:
                            count += 1
                            yield row
            else:
                # Sync all accessible documents in workspace
                try:
                    docs_iter = outline_client.iter_documents()
                except Exception as exc:
                    logger.error("Outline: failed to fetch documents: %s", exc)
                    raise

                for doc in docs_iter:
                    row = _document_to_row(outline_client.base_url, doc)
                    if row:
                        count += 1
                        yield row

            logger.info("Outline: synced %d document(s).", count)
        finally:
            if client is None:
                outline_client.close()

    setattr(outline_documents, DOCUMENT_SOURCE_ATTR, OUTLINE_SOURCE_NAME)

    @dlt.source(name=OUTLINE_SOURCE_NAME)
    def _outline():
        return outline_documents

    source = _outline()
    setattr(source, DOCUMENT_SOURCE_ATTR, OUTLINE_SOURCE_NAME)
    return source


def _document_to_row(base_url: str, doc: dict[str, Any]) -> dict[str, Any]:
    """Transform an Outline document into a standardized Cognee document row."""
    doc_id = doc.get("id", "")
    if not doc_id:
        return {}

    title = doc.get("title") or f"Outline Document {doc_id}"
    url = doc.get("url") or f"{base_url.removesuffix('/api')}/doc/{doc.get('slug', doc_id)}"
    content = _render_document_content(doc) or title

    return {
        "id": f"outline:doc:{doc_id}",
        "url": url,
        "title": title,
        "content": content,
    }


def _sanitize_value(val: Any) -> Any:
    """Recursively redact sensitive field names in nested dictionaries and lists."""
    if isinstance(val, dict):
        return {
            k: _sanitize_value(v)
            for k, v in val.items()
            if k.lower() not in _SENSITIVE_FIELD_NAMES
        }
    if isinstance(val, list):
        return [_sanitize_value(item) for item in val]
    return val


def _render_document_content(doc: dict[str, Any]) -> str:
    """Format Outline document markdown body and metadata for cognify."""
    lines: list[str] = []

    # Outline documents provide Markdown directly in the 'text' property
    body = doc.get("text") or ""
    if body.strip():
        lines.append(body.strip())
        lines.append("")

    metadata_lines: list[str] = []
    for field in ("collectionId", "createdAt", "updatedAt", "publishedAt", "archivedAt"):
        val = doc.get(field)
        if val and isinstance(val, str) and val.strip():
            metadata_lines.append(f"{field}: {val.strip()}")

    # Additional custom attributes/metadata if present
    custom_meta = doc.get("metadata")
    if isinstance(custom_meta, dict):
        clean_meta = _sanitize_value(custom_meta)
        metadata_lines.append(f"metadata: {json.dumps(clean_meta, sort_keys=True)}")

    if metadata_lines:
        if lines:
            lines.append("---")
        lines.extend(metadata_lines)

    return "\n".join(lines).strip()
