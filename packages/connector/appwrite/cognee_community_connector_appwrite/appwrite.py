"""Appwrite data-source connector for cognee.

Extracts Appwrite Database documents, transforms them into markdown documents,
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

logger = get_logger("appwrite_connector")

APPWRITE_TABLE_NAME = "appwrite_documents"
APPWRITE_SOURCE_NAME = "appwrite"

_MAX_RETRIES = 5
_PER_PAGE = 50
_SENSITIVE_FIELD_NAMES = {
    "password",
    "password_hash",
    "hash",
    "token",
    "access_token",
    "refresh_token",
    "secret",
    "client_secret",
    "api_key",
    "apikey",
    "salt",
    "auth_data",
}


class AppwriteClient:
    """Lightweight HTTP client for the Appwrite REST API."""

    def __init__(
        self,
        endpoint: str = "https://cloud.appwrite.io/v1",
        project_id: str | None = None,
        api_key: str | None = None,
        http_client: Any = None,
    ):
        import httpx

        self.endpoint = endpoint.rstrip("/")
        self.project_id = project_id
        self.api_key = api_key

        headers = {
            "Accept": "application/json",
        }
        if self.project_id:
            headers["X-Appwrite-Project"] = self.project_id
        if self.api_key:
            headers["X-Appwrite-Key"] = self.api_key

        self.client = http_client or httpx.Client(
            base_url=self.endpoint,
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

    def request(self, method: str, path: str, **kwargs) -> dict[str, Any]:
        """Execute HTTP request with retries on rate limits or transient errors."""
        for attempt in range(_MAX_RETRIES):
            try:
                response = self.client.request(method, path, **kwargs)
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
                    "Appwrite: %s — retrying in %.1fs (%d/%d).",
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

    def iter_documents(
        self,
        database_id: str,
        collection_id: str,
        custom_queries: list[str] | None = None,
    ) -> Iterator[dict[str, Any]]:
        """Paginate and yield Appwrite documents for a collection."""
        offset = 0
        limit = _PER_PAGE

        while True:
            queries: list[str] = [
                f'limit({limit})',
                f'offset({offset})',
            ]
            if custom_queries:
                queries.extend(custom_queries)

            # Query params formatted for Appwrite REST API: queries[]=...
            params: list[tuple[str, str]] = [("queries[]", q) for q in queries]

            path = f"/databases/{database_id}/collections/{collection_id}/documents"
            data = self.request("GET", path, params=params)
            documents = data.get("documents") or []
            total = data.get("total") or 0

            if not documents:
                break

            yield from documents

            offset += len(documents)
            if offset >= total:
                break


def appwrite_source(
    database_id: str,
    collection_ids: list[str],
    endpoint: str | None = None,
    project_id: str | None = None,
    api_key: str | None = None,
    queries: list[str] | None = None,
    client: AppwriteClient | None = None,
):
    """Create a dlt source ingesting Appwrite collection documents into Cognee.

    Args:
        database_id: Appwrite database ID.
        collection_ids: List of Appwrite collection IDs to ingest.
        endpoint: Appwrite API endpoint (default: ``https://cloud.appwrite.io/v1``).
        project_id: Appwrite Project ID.
        api_key: Appwrite Server API key (needs ``documents.read`` scope).
        queries: Optional additional Appwrite queries (e.g. ``['equal("status", "published")']``).
        client: Pre-configured ``AppwriteClient`` instance (e.g. for testing).

    Returns:
        A dlt source ready for ``cognee.remember(...)`` or ``cognee.add(...)``.
    """
    try:
        import dlt
    except ImportError as exc:
        raise ImportError(
            'The Appwrite connector requires dlt: run "pip install dlt[sqlalchemy]"'
        ) from exc

    resolved_endpoint = (
        endpoint
        or os.environ.get("APPWRITE_ENDPOINT")
        or "https://cloud.appwrite.io/v1"
    )
    resolved_project_id = project_id or os.environ.get("APPWRITE_PROJECT_ID")
    resolved_api_key = api_key or os.environ.get("APPWRITE_API_KEY")

    if client is None:
        if not resolved_project_id:
            raise ValueError(
                "Appwrite Project ID required: pass project_id= or set APPWRITE_PROJECT_ID."
            )
        if not resolved_api_key:
            raise ValueError(
                "Appwrite API Key required: pass api_key= or set APPWRITE_API_KEY."
            )

    appwrite_client = client or AppwriteClient(
        endpoint=resolved_endpoint,
        project_id=resolved_project_id,
        api_key=resolved_api_key,
    )

    @dlt.resource(
        name=APPWRITE_TABLE_NAME,
        primary_key="id",
        write_disposition="replace",
    )
    def appwrite_documents():
        try:
            count = 0
            for collection_id in collection_ids:
                try:
                    docs_iter = appwrite_client.iter_documents(
                        database_id=database_id,
                        collection_id=collection_id,
                        custom_queries=queries,
                    )
                except Exception as exc:
                    logger.error(
                        "Appwrite: failed to query collection %s: %s",
                        collection_id,
                        exc,
                    )
                    raise

                for doc in docs_iter:
                    row = _document_to_row(
                        endpoint=appwrite_client.endpoint,
                        database_id=database_id,
                        collection_id=collection_id,
                        doc=doc,
                    )
                    if row:
                        count += 1
                        yield row

            logger.info("Appwrite: synced %d document(s).", count)
        finally:
            if client is None:
                appwrite_client.close()

    @dlt.source(name=APPWRITE_SOURCE_NAME)
    def _appwrite():
        return appwrite_documents

    source = _appwrite()
    setattr(source, DOCUMENT_SOURCE_ATTR, APPWRITE_SOURCE_NAME)
    return source


def _document_to_row(
    endpoint: str,
    database_id: str,
    collection_id: str,
    doc: dict[str, Any],
) -> dict[str, Any]:
    """Transform an Appwrite document into a standardized Cognee document row."""
    doc_id = doc.get("$id", "")
    if not doc_id:
        return {}

    title = _extract_document_title(doc, doc_id)
    url = f"{endpoint}/databases/{database_id}/collections/{collection_id}/documents/{doc_id}"
    content = _render_document_content(doc, title) or title

    return {
        "id": f"appwrite:{database_id}:{collection_id}:{doc_id}",
        "url": url,
        "title": title,
        "content": content,
    }


def _extract_document_title(doc: dict[str, Any], doc_id: str) -> str:
    """Extract a human-readable title from common title/name attribute fields."""
    for field in ("title", "name", "headline", "subject", "label"):
        val = doc.get(field)
        if isinstance(val, str) and val.strip():
            return val.strip()
    return f"Appwrite Document {doc_id}"


def _render_document_content(doc: dict[str, Any], title: str) -> str:
    """Format Appwrite document attributes into clean markdown for cognify."""
    lines: list[str] = [f"# {title}", ""]

    # Body fields
    body_parts: list[str] = []
    for field in ("content", "body", "text", "description", "details", "message"):
        val = doc.get(field)
        if isinstance(val, str) and val.strip():
            body_parts.append(val.strip())

    if body_parts:
        lines.append("\n\n".join(body_parts))
        lines.append("")

    # Metadata attributes
    metadata_lines: list[str] = []
    for k, v in doc.items():
        if k.startswith("$permissions"):
            continue
        if k.lower() in _SENSITIVE_FIELD_NAMES:
            continue
        if k in ("content", "body", "text", "description", "details", "message", "title", "name"):
            continue

        if isinstance(v, (str, int, float, bool)):
            metadata_lines.append(f"{k}: {v}")
        elif isinstance(v, list) and v and isinstance(v[0], (str, int, float)):
            metadata_lines.append(f"{k}: {', '.join(str(item) for item in v)}")
        elif isinstance(v, dict):
            # Mask sensitive values in nested dicts
            clean_dict = {
                dk: dv
                for dk, dv in v.items()
                if dk.lower() not in _SENSITIVE_FIELD_NAMES
            }
            metadata_lines.append(f"{k}: {json.dumps(clean_dict, sort_keys=True)}")

    if metadata_lines:
        lines.append("---")
        lines.extend(metadata_lines)

    return "\n".join(lines).strip()
