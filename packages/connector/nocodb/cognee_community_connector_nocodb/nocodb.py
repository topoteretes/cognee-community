"""DLT source for NocoDB tables & smart spreadsheet records (full-snapshot sync + forget-on-delete).

Fetches relational table records, field metadata, and cell values from NocoDB,
then formats them as structured markdown documents for cognee's ingestion pipeline.

Declares ``DOCUMENT_SOURCE_ATTR = "nocodb"``, routing table records through Cognee's
standard cognify entity-extraction pipeline into the memory graph.

The source defaults to full snapshot replacement: ``write_disposition="replace"`` rewrites
staging with currently active table rows. Deleted or archived rows drop out of the
active snapshot and cognee's existing ``orphan_cleanup`` purges them from the knowledge graph.
"""

from __future__ import annotations

import hashlib
import math
import os
import time
from typing import Any, Iterable

import httpx
from cognee.shared.logging_utils import get_logger
from cognee.tasks.ingestion.dlt_utils import DOCUMENT_SOURCE_ATTR

logger = get_logger("nocodb_connector")

NOCODB_TABLE_NAME = "nocodb_records"
DEFAULT_BASE_URL = "http://localhost:8080"


def get_retry_delay(response: httpx.Response, attempt: int, base_delay: float = 1.0) -> float:
    """Calculate backoff delay from Retry-After header or exponential backoff."""
    retry_after = response.headers.get("Retry-After")
    if retry_after:
        try:
            delay = float(retry_after)
            if math.isfinite(delay) and delay >= 0:
                return delay
        except (ValueError, TypeError):
            pass
    return base_delay * (2**attempt)


class NocoDBClient:
    """HTTP client for NocoDB REST API v2 with exponential backoff on HTTP 429 and 5xx."""

    def __init__(
        self,
        api_token: str | None = None,
        base_url: str = DEFAULT_BASE_URL,
        transport: httpx.BaseTransport | None = None,
        timeout: float = 30.0,
        max_retries: int = 3,
    ) -> None:
        self.api_token = api_token or os.getenv("NOCODB_API_TOKEN")
        if not self.api_token:
            raise ValueError(
                "NocoDB API token is required. Set NOCODB_API_TOKEN env var or pass api_token."
            )
        self.base_url = (base_url or DEFAULT_BASE_URL).rstrip("/")
        self.max_retries = max_retries

        headers = {
            "xc-token": self.api_token,
            "Content-Type": "application/json",
            "User-Agent": "cognee-community-connector-nocodb/0.1.0",
        }
        self.client = httpx.Client(
            base_url=self.base_url,
            headers=headers,
            transport=transport,
            timeout=timeout,
        )

    def close(self) -> None:
        self.client.close()

    def __enter__(self) -> NocoDBClient:
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
            else f"{self.base_url}/api/v2/{endpoint.lstrip('/')}"
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
                        "NocoDB request to %s returned %d. Backing off for %.2fs (attempt %d/%d)",
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
                    "Network error connecting to NocoDB: %s. Retrying in %.2fs (attempt %d/%d)",
                    exc,
                    delay,
                    attempt + 1,
                    self.max_retries,
                )
                time.sleep(delay)

        if last_exc:
            raise last_exc
        raise RuntimeError("Unexpected failure in get_with_retry")

    def list_records(
        self,
        table_id: str,
        offset: int = 0,
        limit: int = 100,
        where: str | None = None,
        view_id: str | None = None,
    ) -> dict[str, Any]:
        """Fetch a page of records from a NocoDB table."""
        endpoint = f"tables/{table_id}/records"
        params: dict[str, Any] = {"offset": offset, "limit": limit}
        if where:
            params["where"] = where
        if view_id:
            params["viewId"] = view_id

        return self.get_with_retry(endpoint, params=params)


def record_to_document(record: dict[str, Any], table_id: str) -> dict[str, Any]:
    """Convert a NocoDB row record into a rich Markdown document for cognify."""
    record_id = str(record.get("Id") or record.get("id") or record.get("_id") or "")

    # Identify primary candidate title
    primary_title = None
    for candidate_key in ("Title", "title", "Name", "name", "Label", "label", "Summary"):
        if candidate_key in record and record[candidate_key]:
            primary_title = str(record[candidate_key])
            break
    if not primary_title:
        primary_title = f"NocoDB Row #{record_id}"

    created_at = str(record.get("CreatedAt") or record.get("created_at") or "")
    updated_at = str(record.get("UpdatedAt") or record.get("updated_at") or created_at)

    field_lines: list[str] = []
    metadata_fields: dict[str, Any] = {}

    for k, v in record.items():
        if k in ("Id", "id", "_id", "nc_record_id"):
            continue
        if v is not None and v != "":
            field_lines.append(f"- **{k}**: {v}")
            metadata_fields[k] = v

    doc_parts = [
        f"# {primary_title}",
        "",
        f"- **Table ID**: {table_id}",
        f"- **Record ID**: {record_id}",
    ]
    if created_at:
        doc_parts.append(f"- **Created At**: {created_at}")
    if updated_at:
        doc_parts.append(f"- **Updated At**: {updated_at}")

    if field_lines:
        doc_parts.extend(["", "## Record Fields", *field_lines])

    markdown_text = "\n".join(doc_parts)
    raw_hash = hashlib.sha256(f"{table_id}_{record_id}_{updated_at}".encode("utf-8")).hexdigest()

    return {
        "id": f"{table_id}_{record_id}",
        "record_id": record_id,
        "table_id": table_id,
        "title": primary_title,
        "updated_at": updated_at,
        "text": markdown_text,
        "content": markdown_text,
        "raw_hash": raw_hash,
        "metadata": {
            "source": "nocodb",
            "table_id": table_id,
            "record_id": record_id,
            "title": primary_title,
            "updated_at": updated_at,
        },
    }


def fetch_nocodb_records(
    table_ids: list[str] | str,
    api_token: str | None = None,
    base_url: str = DEFAULT_BASE_URL,
    incremental: bool = False,
    page_size: int = 100,
    transport: httpx.BaseTransport | None = None,
) -> Iterable[dict[str, Any]]:
    """Yield NocoDB row documents for dlt ingestion."""
    import dlt

    if isinstance(table_ids, str):
        tables = [table_ids]
    else:
        tables = list(table_ids)

    state = dlt.current.resource_state() if incremental else {}
    last_watermark = state.get("last_updated_after") if incremental else None

    client = NocoDBClient(
        api_token=api_token,
        base_url=base_url,
        transport=transport,
    )

    max_updated = last_watermark

    try:
        for tid in tables:
            offset = 0
            where_clause = None
            if incremental and last_watermark:
                where_clause = f"(UpdatedAt,gt,{last_watermark})"
            while True:
                response = client.list_records(
                    table_id=tid,
                    offset=offset,
                    limit=page_size,
                    where=where_clause,
                )
                records = response.get("list") or response.get("records") or []
                if not records:
                    break

                for rec in records:
                    doc = record_to_document(rec, tid)
                    rec_updated = doc["updated_at"]
                    if rec_updated and (max_updated is None or rec_updated > max_updated):
                        max_updated = rec_updated

                    yield doc

                page_info = response.get("pageInfo", {})
                is_last_page = page_info.get("isLastPage")
                if is_last_page or len(records) < page_size:
                    break
                offset += page_size

        if incremental and max_updated:
            state["last_updated_after"] = max_updated
    finally:
        client.close()


def nocodb_source(
    table_ids: list[str] | str = "table_default",
    api_token: str | None = None,
    base_url: str = DEFAULT_BASE_URL,
    incremental: bool = False,
    page_size: int = 100,
    transport: httpx.BaseTransport | None = None,
) -> Any:
    """Create a dlt source for NocoDB table records."""
    import dlt

    @dlt.resource(
        name=NOCODB_TABLE_NAME,
        write_disposition="merge" if incremental else "replace",
        primary_key="id",
    )
    def records() -> Iterable[dict[str, Any]]:
        yield from fetch_nocodb_records(
            table_ids=table_ids,
            api_token=api_token,
            base_url=base_url,
            incremental=incremental,
            page_size=page_size,
            transport=transport,
        )

    @dlt.source(name="nocodb")
    def source() -> Any:
        return records

    created_source = source()
    setattr(created_source, DOCUMENT_SOURCE_ATTR, "nocodb")
    return created_source
