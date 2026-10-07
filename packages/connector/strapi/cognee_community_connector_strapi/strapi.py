"""DLT source for Strapi CMS content (full-snapshot sync + forget-on-delete).

Fetches content entries, documentation, and articles from Strapi headless CMS,
then formats them as structured markdown documents for cognee's ingestion pipeline.

Declares ``DOCUMENT_SOURCE_ATTR = "strapi"``, routing content entries through Cognee's
standard cognify entity-extraction pipeline into the memory graph.

The source defaults to full snapshot replacement: ``write_disposition="replace"`` rewrites
staging with currently published entries. Deleted or unpublished articles drop out of the
active snapshot and cognee's existing ``orphan_cleanup`` purges them from the knowledge graph.
"""

from __future__ import annotations

import hashlib
import os
import time
from typing import Any, Iterable

import httpx
from cognee.shared.logging_utils import get_logger
from cognee.tasks.ingestion.dlt_utils import DOCUMENT_SOURCE_ATTR

logger = get_logger("strapi_connector")

STRAPI_TABLE_NAME = "strapi_entries"
DEFAULT_BASE_URL = "http://localhost:1337"


def get_retry_delay(response: httpx.Response, attempt: int, base_delay: float = 1.0) -> float:
    """Calculate backoff delay from Retry-After header or exponential backoff."""
    retry_after = response.headers.get("Retry-After")
    if retry_after:
        try:
            return float(retry_after)
        except ValueError:
            pass
    return base_delay * (2**attempt)


class StrapiClient:
    """HTTP client for Strapi REST API with exponential backoff on HTTP 429 and 5xx."""

    def __init__(
        self,
        api_token: str | None = None,
        base_url: str = DEFAULT_BASE_URL,
        transport: httpx.BaseTransport | None = None,
        timeout: float = 30.0,
        max_retries: int = 3,
    ) -> None:
        self.api_token = api_token or os.getenv("STRAPI_API_TOKEN")
        if not self.api_token:
            raise ValueError(
                "Strapi API token is required. Set STRAPI_API_TOKEN env var or pass api_token."
            )
        self.base_url = (base_url or DEFAULT_BASE_URL).rstrip("/")
        self.max_retries = max_retries

        headers = {
            "Authorization": f"Bearer {self.api_token}",
            "Content-Type": "application/json",
            "User-Agent": "cognee-community-connector-strapi/0.1.0",
        }
        self.client = httpx.Client(
            base_url=self.base_url,
            headers=headers,
            transport=transport,
            timeout=timeout,
        )

    def close(self) -> None:
        self.client.close()

    def __enter__(self) -> StrapiClient:
        return self

    def __exit__(self, *args: Any) -> None:
        self.close()

    def get_with_retry(
        self,
        endpoint: str,
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Execute GET request with retry backoff for rate limits and server errors."""
        url = (
            endpoint
            if endpoint.startswith("http")
            else f"{self.base_url}/api/{endpoint.lstrip('/')}"
        )
        last_exc: Exception | None = None

        for attempt in range(self.max_retries + 1):
            try:
                response = self.client.get(url, params=params)
                if response.status_code == 429 or response.status_code >= 500:
                    if attempt == self.max_retries:
                        response.raise_for_status()
                    delay = get_retry_delay(response, attempt)
                    logger.warning(
                        "Strapi request to %s returned %d. Backing off for %.2fs (attempt %d/%d)",
                        url,
                        response.status_code,
                        delay,
                        attempt + 1,
                        self.max_retries,
                    )
                    time.sleep(delay)
                    continue

                response.raise_for_status()
                return response.json()
            except (httpx.NetworkError, httpx.TimeoutException) as exc:
                last_exc = exc
                if attempt == self.max_retries:
                    raise
                delay = 1.0 * (2**attempt)
                logger.warning(
                    "Network error connecting to Strapi: %s. Retrying in %.2fs (attempt %d/%d)",
                    exc,
                    delay,
                    attempt + 1,
                    self.max_retries,
                )
                time.sleep(delay)

        if last_exc:
            raise last_exc
        raise RuntimeError("Unexpected failure in get_with_retry")

    def list_entries(
        self,
        content_type: str,
        page: int = 1,
        page_size: int = 100,
        updated_after: str | None = None,
        publication_state: str = "live",
    ) -> dict[str, Any]:
        """Fetch a paginated page of content collection entries from Strapi."""
        endpoint = content_type.lstrip("/")
        params: dict[str, Any] = {
            "populate": "*",
            "pagination[page]": page,
            "pagination[pageSize]": page_size,
            "publicationState": publication_state,
        }
        if updated_after:
            params["filters[updatedAt][$gt]"] = updated_after

        return self.get_with_retry(endpoint, params=params)


def extract_entry_fields(entry: dict[str, Any]) -> dict[str, Any]:
    """Normalize entry across Strapi v4 (attributes wrapped) and v5 (flat) formats."""
    entry_id = str(entry.get("id") or entry.get("documentId") or "")
    if "attributes" in entry and isinstance(entry["attributes"], dict):
        attrs = entry["attributes"]
    else:
        attrs = entry

    title = (
        attrs.get("title")
        or attrs.get("name")
        or attrs.get("headline")
        or f"Strapi Entry #{entry_id}"
    )
    content = (
        attrs.get("content")
        or attrs.get("body")
        or attrs.get("description")
        or attrs.get("text")
        or ""
    )
    published_at = attrs.get("publishedAt") or ""
    updated_at = attrs.get("updatedAt") or attrs.get("createdAt") or ""

    # Extract author if present
    author_data = attrs.get("author") or {}
    author_name = ""
    if isinstance(author_data, dict):
        author_attrs = author_data.get("data", {}).get("attributes", {}) or author_data
        author_name = author_attrs.get("name") or author_attrs.get("username") or ""

    # Extract category if present
    cat_data = attrs.get("category") or {}
    category_name = ""
    if isinstance(cat_data, dict):
        cat_attrs = cat_data.get("data", {}).get("attributes", {}) or cat_data
        category_name = cat_attrs.get("name") or cat_attrs.get("title") or ""

    return {
        "id": entry_id,
        "title": title,
        "content": content,
        "published_at": published_at,
        "updated_at": updated_at,
        "author_name": author_name,
        "category_name": category_name,
    }


def strapi_entry_to_document(entry: dict[str, Any], content_type: str) -> dict[str, Any]:
    """Convert a Strapi entry into a rich Markdown document for cognify."""
    fields = extract_entry_fields(entry)
    entry_id = fields["id"]
    title = fields["title"]
    body = fields["content"]
    published_at = fields["published_at"]
    updated_at = fields["updated_at"]
    author = fields["author_name"]
    category = fields["category_name"]

    doc_parts = [
        f"# {title}",
        "",
        f"- **Content Type**: {content_type}",
        f"- **Entry ID**: {entry_id}",
    ]
    if published_at:
        doc_parts.append(f"- **Published At**: {published_at}")
    if updated_at:
        doc_parts.append(f"- **Updated At**: {updated_at}")
    if author:
        doc_parts.append(f"- **Author**: {author}")
    if category:
        doc_parts.append(f"- **Category**: {category}")

    doc_parts.extend(["", "## Content", body if body else "*(No content body provided)*"])

    markdown_text = "\n".join(doc_parts)
    raw_hash = hashlib.sha256(f"{entry_id}_{updated_at}".encode("utf-8")).hexdigest()

    return {
        "id": f"{content_type}_{entry_id}",
        "entry_id": entry_id,
        "content_type": content_type,
        "title": title,
        "updated_at": updated_at,
        "text": markdown_text,
        "content": markdown_text,
        "raw_hash": raw_hash,
        "metadata": {
            "source": "strapi",
            "content_type": content_type,
            "entry_id": entry_id,
            "title": title,
            "category": category,
        },
    }


def fetch_strapi_entries(
    content_types: list[str] | str,
    api_token: str | None = None,
    base_url: str = DEFAULT_BASE_URL,
    incremental: bool = False,
    page_size: int = 100,
    transport: httpx.BaseTransport | None = None,
) -> Iterable[dict[str, Any]]:
    """Yield Strapi documents for dlt ingestion."""
    import dlt

    if isinstance(content_types, str):
        types_to_fetch = [content_types]
    else:
        types_to_fetch = list(content_types)

    state = dlt.current.resource_state() if incremental else {}
    last_watermark = state.get("last_updated_after") if incremental else None

    client = StrapiClient(
        api_token=api_token,
        base_url=base_url,
        transport=transport,
    )

    max_updated = last_watermark

    try:
        for ctype in types_to_fetch:
            page = 1
            while True:
                response = client.list_entries(
                    content_type=ctype,
                    page=page,
                    page_size=page_size,
                    updated_after=last_watermark,
                )
                data = response.get("data", [])
                if not data:
                    break

                for entry in data:
                    doc = strapi_entry_to_document(entry, ctype)
                    entry_updated = doc["updated_at"]
                    if entry_updated and (max_updated is None or entry_updated > max_updated):
                        max_updated = entry_updated

                    yield doc

                meta = response.get("meta", {}).get("pagination", {})
                page_count = meta.get("pageCount")
                if page_count and page >= page_count:
                    break
                if len(data) < page_size:
                    break
                page += 1

        if incremental and max_updated:
            state["last_updated_after"] = max_updated
    finally:
        client.close()


def strapi_source(
    content_types: list[str] | str = "articles",
    api_token: str | None = None,
    base_url: str = DEFAULT_BASE_URL,
    incremental: bool = False,
    page_size: int = 100,
    transport: httpx.BaseTransport | None = None,
) -> Any:
    """Create a dlt source for Strapi CMS content."""
    import dlt

    @dlt.resource(
        name=STRAPI_TABLE_NAME,
        write_disposition="merge" if incremental else "replace",
        primary_key="id",
    )
    def entries() -> Iterable[dict[str, Any]]:
        yield from fetch_strapi_entries(
            content_types=content_types,
            api_token=api_token,
            base_url=base_url,
            incremental=incremental,
            page_size=page_size,
            transport=transport,
        )

    @dlt.source(name="strapi")
    def source() -> Any:
        return entries

    created_source = source()
    setattr(created_source, DOCUMENT_SOURCE_ATTR, "strapi")
    return created_source
