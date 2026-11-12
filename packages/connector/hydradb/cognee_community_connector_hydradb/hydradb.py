"""HydraDB / HydeaDB data-source connector for cognee.

Extracts documents, contextual knowledge, and memory records from HydraDB
into cognee knowledge graphs for entity extraction and retrieval.

Usage::

    import cognee
    from cognee_community_connector_hydradb import hydradb_source

    await cognee.remember(
        hydradb_source(
            api_key="...",
            database_id="production",
            collection_id="tech_specs",
        ),
        dataset_name="hydradb_knowledge",
    )
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Generator
from typing import Any

logger = logging.getLogger(__name__)

try:
    from cognee.tasks.ingestion.dlt_utils import DOCUMENT_SOURCE_ATTR
except ImportError:
    DOCUMENT_SOURCE_ATTR = "cognee_document_source"

HYDRADB_API_BASE = "https://api.hydradb.com/v2"


class HydraDBError(Exception):
    """Base exception for HydraDB connector errors."""

    pass


class HydraDBAPIError(HydraDBError):
    """Raised when the HydraDB API returns an HTTP or network error."""

    pass


class HydraDBAuthError(HydraDBAPIError):
    """Raised on authentication or permission failure (401/403)."""

    pass


class HydraDBNotFoundError(HydraDBAPIError):
    """Raised when a requested database, collection, or context does not exist (404)."""

    pass


class HydraDBRateLimitError(HydraDBAPIError):
    """Raised when API rate limits are exceeded (429)."""

    pass


class HydraDBClient:
    """HTTP client for querying HydraDB REST API v2 endpoints."""

    def __init__(
        self,
        api_key: str | None = None,
        base_url: str = HYDRADB_API_BASE,
        timeout: int = 15,
        max_retries: int = 3,
    ) -> None:
        self.api_key = api_key or os.environ.get("HYDRADB_API_KEY")
        self.base_url = (base_url or HYDRADB_API_BASE).rstrip("/")
        self.timeout = timeout
        self.max_retries = max_retries

    def _get_headers(self) -> dict[str, str]:
        headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "User-Agent": "cognee-community-connector-hydradb/0.1.0",
        }
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
            headers["X-Hydra-Key"] = self.api_key
        return headers

    def ping(self) -> bool:
        """Checks if the HydraDB API is reachable with configured credentials."""
        try:
            res = self.get("/health")
            return bool(res.get("status") in ("ok", "healthy") or res.get("healthy", False) or res)
        except Exception:
            return False

    def get(self, endpoint: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        """Executes an authenticated GET request with backoff retry."""
        query_string = ""
        if params:
            filtered_params = {k: v for k, v in params.items() if v is not None}
            if filtered_params:
                query_string = "?" + urllib.parse.urlencode(filtered_params)

        url = f"{self.base_url}{endpoint}{query_string}"
        headers = self._get_headers()

        for attempt in range(self.max_retries):
            try:
                req = urllib.request.Request(url, headers=headers, method="GET")
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    raw_data = resp.read().decode("utf-8")
                    return json.loads(raw_data) if raw_data else {}
            except urllib.error.HTTPError as e:
                err_text = e.read().decode("utf-8", errors="replace")
                if e.code in (401, 403):
                    msg = f"HydraDB authentication failed ({e.code}): {err_text}"
                    raise HydraDBAuthError(msg) from e
                if e.code == 404:
                    msg = f"HydraDB resource not found (404): {err_text}"
                    raise HydraDBNotFoundError(msg) from e
                if e.code == 429:
                    if attempt < self.max_retries - 1:
                        time.sleep(1.5 * (attempt + 1))
                        continue
                    msg = f"HydraDB rate limit exceeded (429): {err_text}"
                    raise HydraDBRateLimitError(msg) from e
                if e.code >= 500 and attempt < self.max_retries - 1:
                    time.sleep(1.0 * (attempt + 1))
                    continue
                raise HydraDBAPIError(f"HydraDB API error ({e.code}): {err_text}") from e
            except urllib.error.URLError as e:
                if attempt < self.max_retries - 1:
                    time.sleep(1.0 * (attempt + 1))
                    continue
                raise HydraDBAPIError(f"Network error connecting to HydraDB: {e.reason}") from e

        return {}

    def list_contexts(
        self,
        database_id: str | None = None,
        collection_id: str | None = None,
        category: str | None = None,
        page: int = 1,
        limit: int = 50,
    ) -> dict[str, Any]:
        """Lists contexts with optional database and collection filters."""
        endpoint = "/contexts"
        if database_id and collection_id:
            endpoint = f"/databases/{database_id}/collections/{collection_id}/contexts"
        elif database_id:
            endpoint = f"/databases/{database_id}/contexts"

        params: dict[str, Any] = {"page": page, "limit": limit}
        if category:
            params["category"] = category

        return self.get(endpoint, params=params)

    def inspect_context(self, context_id: str) -> dict[str, Any]:
        """Fetches full content, payload, and metadata for a specific context record."""
        return self.get(f"/contexts/{context_id}")


def _compute_hash(content: str) -> str:
    """Computes SHA-256 fingerprint for document content."""
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def fetch_hydradb_contexts(
    api_key: str | None = None,
    database_id: str | None = None,
    collection_id: str | None = None,
    category: str | None = None,
    client: HydraDBClient | None = None,
    limit_per_page: int = 50,
    max_pages: int = 100,
) -> Generator[dict[str, Any], None, None]:
    """Generator yielding normalized HydraDB context records for Cognee memory graphs."""
    if client is None:
        client = HydraDBClient(api_key=api_key)

    page = 1
    total_yielded = 0

    while page <= max_pages:
        resp = client.list_contexts(
            database_id=database_id,
            collection_id=collection_id,
            category=category,
            page=page,
            limit=limit_per_page,
        )

        contexts = resp.get("contexts") or resp.get("data") or []
        if not contexts:
            break

        for item in contexts:
            cid = str(item.get("id") or item.get("context_id") or "")
            if not cid:
                continue

            title = str(item.get("title") or item.get("name") or "Untitled Context")
            db_name = str(item.get("database_id") or database_id or "default")
            coll_name = str(item.get("collection_id") or collection_id or "default")
            cat_name = str(item.get("category") or category or "knowledge")
            created_at = item.get("created_at") or time.time()
            updated_at = item.get("updated_at") or created_at

            # Fetch detailed content if not present in listing
            raw_text = item.get("content") or item.get("text") or ""
            if not raw_text:
                try:
                    detailed = client.inspect_context(cid)
                    raw_text = detailed.get("content") or detailed.get("text") or ""
                    if not raw_text and "payload" in detailed:
                        raw_text = json.dumps(detailed["payload"], indent=2)
                except Exception as e:
                    logger.warning("Could not inspect HydraDB context %s: %s", cid, e)
                    raw_text = str(item.get("summary") or "")

            # Compose formatted markdown text representation for Cognee knowledge graph
            header_lines = [
                f"# Context: {title}",
                f"- Database: {db_name}",
                f"- Collection: {coll_name}",
                f"- Category: {cat_name}",
                f"- Record ID: {cid}",
            ]
            if raw_text:
                header_lines.append(f"\n## Content:\n{raw_text}")

            formatted_doc = "\n".join(header_lines)
            content_fingerprint = _compute_hash(formatted_doc)
            deterministic_id = f"hydradb:{db_name}:{coll_name}:{cid}"

            yield {
                "id": deterministic_id,
                "context_id": cid,
                "title": title,
                "database": db_name,
                "collection": coll_name,
                "category": cat_name,
                "created_at": created_at,
                "updated_at": updated_at,
                "content_hash": content_fingerprint,
                "text": formatted_doc,
                "metadata": {
                    "source": "hydradb",
                    "database_id": db_name,
                    "collection_id": coll_name,
                    "category": cat_name,
                    "context_id": cid,
                },
            }
            total_yielded += 1

        has_more = resp.get("has_more") or (len(contexts) >= limit_per_page)
        if not has_more:
            break
        page += 1


def hydradb_source(
    api_key: str | None = None,
    database_id: str | None = None,
    collection_id: str | None = None,
    category: str | None = None,
    client: HydraDBClient | None = None,
):
    """Factory creating a Cognee/dlt source yielding HydraDB context documents."""
    import dlt

    if client is None:
        client = HydraDBClient(api_key=api_key)

    @dlt.source(name="hydradb")
    def _source():
        @dlt.resource(
            name="hydradb_contexts",
            primary_key="id",
            write_disposition="merge",
        )
        def _resource():
            yield from fetch_hydradb_contexts(
                api_key=api_key,
                database_id=database_id,
                collection_id=collection_id,
                category=category,
                client=client,
            )

        setattr(_resource, DOCUMENT_SOURCE_ATTR, "hydradb")
        return _resource()

    return _source()
