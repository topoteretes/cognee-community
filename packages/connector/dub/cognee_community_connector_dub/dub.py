"""DLT source for Dub.co short links and conversion analytics (full snapshot + forget-on-delete).

Fetches Dub.co managed short links, destination targets, campaign tags, comments,
and click engagement metrics, rendering them into clean Markdown documents.
Yields them as a dlt resource for cognee's ingestion pipeline.

Unlike relational dlt paths, Dub.co link records are ingested as *normal documents*:
the source declares ``cognee_document_source = "dub"``, so ``resolve_dlt_sources``
tags each item with ``external_metadata["source"] = "dub"``. This ensures each
document flows through cognee's standard cognify entity-extraction pipeline —
extracting entities, relationships, and concepts for agent reasoning — instead of
the flat dlt-row schema path.

The source uses a full snapshot synchronization model: ``write_disposition="replace"``
rewrites staging on each run with the links currently active. Deletions propagate
cleanly — an archived or deleted link drops out of the snapshot, so cognee's existing
``orphan_cleanup`` removes it from the graph and vector stores. Unchanged items
preserve stable content-hash ``data_id`` keys, preventing redundant re-ingestion.
"""

from collections.abc import Iterator
import os
import time
from typing import Any

import httpx

from cognee.shared.logging_utils import get_logger
from cognee.tasks.ingestion.dlt_utils import DOCUMENT_SOURCE_ATTR

logger = get_logger("dub_connector")

DUB_TABLE_NAME = "dub_documents"
DUB_SOURCE_NAME = "dub"
DUB_BASE_URL = "https://api.dub.co"

# Retry budget for rate-limited (429) / transient Dub.co API responses
_MAX_RETRIES = 5
_BASE_BACKOFF = 1.0


def _request_with_retry(
    client: httpx.Client,
    method: str,
    url: str,
    headers: dict[str, str],
    params: dict[str, Any] | None = None,
) -> Any:
    """Execute an HTTP request with exponential backoff on 429 rate limits and 5xx errors."""
    for attempt in range(_MAX_RETRIES):
        try:
            response = client.request(method, url, headers=headers, params=params, timeout=30.0)
            if response.status_code == 429:
                retry_after = float(response.headers.get("Retry-After", _BASE_BACKOFF * (2**attempt)))
                logger.warning("Dub.co rate limit reached, waiting %.1f seconds", retry_after)
                time.sleep(retry_after)
                continue
            response.raise_for_status()
            return response.json()
        except (httpx.TransportError, httpx.HTTPStatusError) as exc:
            if attempt == _MAX_RETRIES - 1:
                raise RuntimeError(f"Dub.co request failed after {_MAX_RETRIES} attempts: {exc}") from exc
            time.sleep(_BASE_BACKOFF * (2**attempt))
    raise RuntimeError(f"Dub.co request exceeded retry budget for {url}")


def _format_link_markdown(link: dict[str, Any]) -> str:
    """Render a Dub.co link deterministically to Markdown for entity extraction."""
    l_id = link.get("id", "")
    domain = link.get("domain", "")
    key = link.get("key", "")
    short_link = f"https://{domain}/{key}" if domain and key else link.get("shortLink", "")
    destination_url = link.get("url", "")
    title = (link.get("title") or key or "Untitled Link").strip()
    description = (link.get("description") or "").strip()
    clicks = link.get("clicks", 0)

    # Extract tags deterministically (sorted)
    tags_raw = link.get("tags") or []
    tag_names = sorted([t.get("name") if isinstance(t, dict) else str(t) for t in tags_raw])
    tags_str = ", ".join(tag_names) if tag_names else "None"

    comments = (link.get("comments") or "").strip()

    doc_parts = [
        f"# Dub.co Link: {title}",
        f"- **Link ID**: {l_id}",
        f"- **Short URL**: {short_link}",
        f"- **Destination URL**: {destination_url}",
        f"- **Total Clicks**: {clicks}",
        f"- **Campaign Tags**: {tags_str}",
    ]
    if description:
        doc_parts.append(f"- **Summary**: {description}")

    if comments:
        doc_parts.append("\n## Notes & Comments")
        doc_parts.append(comments)

    return "\n".join(doc_parts)


def dub_source(
    api_key: str | None = None,
    workspace_id: str | None = None,
    tag_ids: list[str] | None = None,
    include_archived: bool = False,
    client: Any = None,
):
    """Create a dlt source that yields Dub.co links and metadata as markdown documents.

    Args:
        api_key: Dub.co API token. Falls back to ``DUB_API_KEY``.
        workspace_id: Dub.co workspace ID. Auto-detected from account if omitted.
        tag_ids: Restrict ingestion to specific tag IDs.
        include_archived: Whether to include archived links (default: False).
        client: Pre-built ``httpx.Client`` (primarily for test mock injection).

    Returns:
        A dlt source suitable for ``cognee.add(...)`` / ``cognee.remember(...)``.
    """
    try:
        import dlt
    except ImportError as exc:
        raise ImportError(
            'The Dub.co connector requires dlt: pip install "dlt[sqlalchemy] httpx"'
        ) from exc

    resolved_token = api_key or os.environ.get("DUB_API_KEY")
    if not resolved_token:
        raise ValueError(
            "Dub.co API token required: pass api_key= or set DUB_API_KEY."
        )

    http_client = client or httpx.Client(timeout=30.0)
    headers = {
        "Authorization": f"Bearer {resolved_token}",
        "Content-Type": "application/json",
    }

    @dlt.resource(name=DUB_TABLE_NAME, primary_key="id", write_disposition="replace")
    def dub_documents() -> Iterator[dict[str, Any]]:
        active_workspace = workspace_id or os.environ.get("DUB_WORKSPACE_ID")
        if not active_workspace:
            workspaces_data = _request_with_retry(http_client, "GET", f"{DUB_BASE_URL}/workspaces", headers)
            workspaces = workspaces_data if isinstance(workspaces_data, list) else workspaces_data.get("workspaces", [])
            if workspaces:
                active_workspace = str(workspaces[0].get("id"))
                logger.info("Auto-detected Dub.co workspace_id: %s", active_workspace)

        page = 1
        page_size = 100

        while True:
            params: dict[str, Any] = {
                "page": page,
                "pageSize": page_size,
                "includeArchived": str(include_archived).lower(),
            }
            if active_workspace:
                params["workspaceId"] = active_workspace
            if tag_ids:
                params["tagIds"] = ",".join(tag_ids)

            links_url = f"{DUB_BASE_URL}/links"
            data = _request_with_retry(http_client, "GET", links_url, headers, params)
            links = data if isinstance(data, list) else data.get("links", [])
            if not links:
                break

            for link in links:
                l_id = str(link.get("id"))
                doc_markdown = _format_link_markdown(link)
                yield {
                    "id": f"link_{l_id}",
                    "key": link.get("key", ""),
                    "url": link.get("url", ""),
                    "type": "short_link",
                    "body": doc_markdown,
                }

            if len(links) < page_size:
                break
            page += 1

    # Declare document source attribute so cognee runs full entity extraction (document-mode)
    setattr(dub_documents, DOCUMENT_SOURCE_ATTR, DUB_SOURCE_NAME)

    @dlt.source(name=DUB_SOURCE_NAME)
    def source():
        return dub_documents

    src_instance = source()
    setattr(src_instance, DOCUMENT_SOURCE_ATTR, DUB_SOURCE_NAME)
    return src_instance
