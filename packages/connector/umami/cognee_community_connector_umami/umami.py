"""DLT source for Umami privacy-preserving analytics and traffic trends (full snapshot + forget-on-delete).

Fetches Umami tracked websites, visitor totals, pageviews, session durations,
bounce statistics, and top page paths, rendering them into clean Markdown documents.
Yields them as a dlt resource for cognee's ingestion pipeline.

Unlike relational dlt paths, Umami analytics reports are ingested as *normal documents*:
the source declares ``cognee_document_source = "umami"``, so ``resolve_dlt_sources``
tags each item with ``external_metadata["source"] = "umami"``. This ensures each
document flows through cognee's standard cognify entity-extraction pipeline —
extracting entities, relationships, and concepts for agent reasoning — instead of
the flat dlt-row schema path.

The source uses a full snapshot synchronization model: ``write_disposition="replace"``
rewrites staging on each run with the websites and analytics currently active. Deletions
propagate cleanly — a removed website drops out of the snapshot, so cognee's existing
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

logger = get_logger("umami_connector")

UMAMI_TABLE_NAME = "umami_documents"
UMAMI_SOURCE_NAME = "umami"
DEFAULT_UMAMI_BASE_URL = "https://api.umami.is/v1"

# Retry budget for rate-limited (429) / transient Umami API responses
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
                logger.warning("Umami rate limit reached, waiting %.1f seconds", retry_after)
                time.sleep(retry_after)
                continue
            response.raise_for_status()
            return response.json()
        except (httpx.TransportError, httpx.HTTPStatusError) as exc:
            if attempt == _MAX_RETRIES - 1:
                raise RuntimeError(f"Umami request failed after {_MAX_RETRIES} attempts: {exc}") from exc
            time.sleep(_BASE_BACKOFF * (2**attempt))
    raise RuntimeError(f"Umami request exceeded retry budget for {url}")


def _format_website_markdown(
    website: dict[str, Any],
    stats: dict[str, Any],
    top_pages: list[dict[str, Any]] | None = None,
) -> str:
    """Render website traffic statistics deterministically to Markdown for entity extraction."""
    w_id = website.get("id", "")
    name = (website.get("name") or "Unnamed Website").strip()
    domain = website.get("domain", "")

    pageviews = (stats.get("pageviews") or {}).get("value", 0) if isinstance(stats.get("pageviews"), dict) else stats.get("pageviews", 0)
    visitors = (stats.get("visitors") or {}).get("value", 0) if isinstance(stats.get("visitors"), dict) else stats.get("visitors", 0)
    visits = (stats.get("visits") or {}).get("value", 0) if isinstance(stats.get("visits"), dict) else stats.get("visits", 0)
    bounces = (stats.get("bounces") or {}).get("value", 0) if isinstance(stats.get("bounces"), dict) else stats.get("bounces", 0)
    totaltime = (stats.get("totaltime") or {}).get("value", 0) if isinstance(stats.get("totaltime"), dict) else stats.get("totaltime", 0)

    # Derived metrics
    bounce_rate = f"{(bounces / visits * 100):.1f}%" if visits and visits > 0 else "N/A"
    avg_duration_sec = f"{(totaltime / visits):.1f}s" if visits and visits > 0 else "N/A"

    doc_parts = [
        f"# Umami Website Analytics: {name}",
        f"- **Website ID**: {w_id}",
        f"- **Domain**: {domain}",
        f"- **Unique Visitors**: {visitors}",
        f"- **Total Pageviews**: {pageviews}",
        f"- **Total Visits**: {visits}",
        f"- **Bounce Rate**: {bounce_rate}",
        f"- **Average Visit Duration**: {avg_duration_sec}",
    ]

    if top_pages:
        doc_parts.append("\n## Top Active Pages")
        for item in top_pages[:10]:
            path = item.get("x") or item.get("url") or "/"
            views = item.get("y") or item.get("views") or 0
            doc_parts.append(f"- **{path}**: {views} pageviews")

    return "\n".join(doc_parts)


def umami_source(
    api_key: str | None = None,
    base_url: str | None = None,
    website_ids: list[str] | None = None,
    client: Any = None,
):
    """Create a dlt source that yields Umami website analytics reports as markdown documents.

    Args:
        api_key: Umami API key. Falls back to ``UMAMI_API_KEY``.
        base_url: Umami API base URL (default: ``https://api.umami.is/v1``).
        website_ids: Restrict ingestion to specific website IDs. When omitted, all websites are ingested.
        client: Pre-built ``httpx.Client`` (primarily for test mock injection).

    Returns:
        A dlt source suitable for ``cognee.add(...)`` / ``cognee.remember(...)``.
    """
    try:
        import dlt
    except ImportError as exc:
        raise ImportError(
            'The Umami connector requires dlt: pip install "dlt[sqlalchemy] httpx"'
        ) from exc

    resolved_key = api_key or os.environ.get("UMAMI_API_KEY")
    if not resolved_key:
        raise ValueError(
            "Umami API key required: pass api_key= or set UMAMI_API_KEY."
        )

    api_base = (base_url or os.environ.get("UMAMI_BASE_URL") or DEFAULT_UMAMI_BASE_URL).rstrip("/")
    http_client = client or httpx.Client(timeout=30.0)
    headers = {
        "x-umami-api-key": resolved_key,
        "Authorization": f"Bearer {resolved_key}",
        "Accept": "application/json",
    }

    @dlt.resource(name=UMAMI_TABLE_NAME, primary_key="id", write_disposition="replace")
    def umami_documents() -> Iterator[dict[str, Any]]:
        # Fetch tracked websites
        websites_url = f"{api_base}/websites"
        data = _request_with_retry(http_client, "GET", websites_url, headers)
        websites = data.get("data") if isinstance(data, dict) and "data" in data else data
        if not isinstance(websites, list):
            websites = []

        now_ms = int(time.time() * 1000)
        # Default window: past 30 days
        thirty_days_ms = 30 * 24 * 60 * 60 * 1000
        start_ms = now_ms - thirty_days_ms

        for site in websites:
            s_id = str(site.get("id"))
            if website_ids and s_id not in website_ids:
                continue

            # Fetch general stats
            stats_url = f"{api_base}/websites/{s_id}/stats"
            stats_params = {"startAt": start_ms, "endAt": now_ms}
            stats = _request_with_retry(http_client, "GET", stats_url, headers, stats_params)

            # Fetch top page paths
            metrics_url = f"{api_base}/websites/{s_id}/metrics"
            metrics_params = {"type": "url", "startAt": start_ms, "endAt": now_ms}
            try:
                top_pages = _request_with_retry(http_client, "GET", metrics_url, headers, metrics_params)
            except Exception:
                top_pages = []

            doc_markdown = _format_website_markdown(site, stats if isinstance(stats, dict) else {}, top_pages if isinstance(top_pages, list) else [])
            yield {
                "id": f"website_{s_id}",
                "name": site.get("name", ""),
                "domain": site.get("domain", ""),
                "type": "analytics_report",
                "body": doc_markdown,
            }

    # Declare document source attribute so cognee runs full entity extraction (document-mode)
    setattr(umami_documents, DOCUMENT_SOURCE_ATTR, UMAMI_SOURCE_NAME)

    @dlt.source(name=UMAMI_SOURCE_NAME)
    def source():
        return umami_documents

    src_instance = source()
    setattr(src_instance, DOCUMENT_SOURCE_ATTR, UMAMI_SOURCE_NAME)
    return src_instance
