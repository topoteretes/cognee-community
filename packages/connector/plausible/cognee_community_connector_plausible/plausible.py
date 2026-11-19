"""DLT source for Plausible Analytics (traffic & conversion metrics into AI knowledge graphs).

Fetches web analytics data from Plausible Analytics (Cloud or self-hosted) and renders
metrics into structured markdown documents for Cognee's ingestion and entity extraction pipeline.

Like Notion and Confluence connectors, Plausible records are ingested as *normal documents*:
the source declares ``cognee_document_source = "plausible"`` via ``DOCUMENT_SOURCE_ATTR``,
so ``resolve_dlt_sources`` tags each row ``external_metadata["source"] = "plausible"``.
Each report flows through Cognee's standard ``cognify()`` pipeline, building an interconnected
knowledge graph of websites, top visited pages, acquisition channels, and conversion goals.

The source operates as a full snapshot: ``write_disposition="replace"`` writes the current
state of analytics. Unchanged reports maintain stable deterministic IDs, while obsolete
or removed sites/periods drop out of the snapshot so Cognee's ``orphan_cleanup`` purges them
from graph and vector stores.
"""

from __future__ import annotations

import os
import time
from typing import Any
from uuid import NAMESPACE_OID, uuid5

import httpx
from cognee.shared.logging_utils import get_logger
from cognee.tasks.ingestion.dlt_utils import DOCUMENT_SOURCE_ATTR

logger = get_logger("plausible_connector")

PLAUSIBLE_SOURCE_NAME = "plausible"
PLAUSIBLE_TABLE_NAME = "plausible_metrics"
DEFAULT_BASE_URL = "https://plausible.io"

_MAX_RETRIES = 5
_DEFAULT_TIMEOUT = 30.0

_EXTRA_HINT = (
    "The Plausible connector requires dlt and httpx: "
    'pip install "cognee-community-connector-plausible"'
)


class PlausibleError(Exception):
    """Base exception for Plausible connector errors."""


class PlausibleAuthError(PlausibleError):
    """Raised when authentication fails (HTTP 401 or 403)."""


class PlausibleAPIError(PlausibleError):
    """Raised when Plausible API returns an unexpected error."""


class PlausibleRateLimitError(PlausibleError):
    """Raised when rate limits are exhausted after retries."""


def _redact_token(message: str, token: str | None) -> str:
    """Sanitize API keys from logs and error messages."""
    if not token or len(token) < 4:
        return message
    return message.replace(token, "[REDACTED_TOKEN]")


class PlausibleClient:
    """HTTP client for Plausible Analytics REST API."""

    def __init__(
        self,
        api_key: str,
        base_url: str = DEFAULT_BASE_URL,
        timeout: float = _DEFAULT_TIMEOUT,
        client: httpx.Client | None = None,
    ) -> None:
        self.api_key = api_key.strip()
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self._client = client or httpx.Client(
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "User-Agent": "cognee-community-connector-plausible/0.1.0",
            },
            timeout=self.timeout,
        )

    def _request(self, endpoint: str, params: dict[str, Any]) -> dict[str, Any]:
        """Execute HTTP GET request with retries and exponential backoff."""
        url = f"{self.base_url}/api/v1/stats{endpoint}"
        retries = 0

        while True:
            try:
                response = self._client.get(url, params=params)

                if response.status_code in (401, 403):
                    msg = (
                        f"Plausible authentication failed ({response.status_code}): {response.text}"
                    )
                    raise PlausibleAuthError(_redact_token(msg, self.api_key))

                if response.status_code == 429:
                    retries += 1
                    if retries > _MAX_RETRIES:
                        raise PlausibleRateLimitError(
                            "Plausible rate limit exceeded after maximum retries."
                        )

                    retry_after = response.headers.get("Retry-After")
                    sleep_time = float(retry_after) if retry_after else (2**retries * 0.5)
                    logger.warning(
                        "Rate limited by Plausible (429). Retrying in %.2fs (attempt %d/%d)...",
                        sleep_time,
                        retries,
                        _MAX_RETRIES,
                    )
                    time.sleep(sleep_time)
                    continue

                if response.status_code >= 400:
                    msg = f"Plausible API error ({response.status_code}): {response.text}"
                    raise PlausibleAPIError(_redact_token(msg, self.api_key))

                return response.json()

            except (httpx.RequestError, httpx.HTTPStatusError) as exc:
                retries += 1
                if retries > _MAX_RETRIES:
                    msg = f"Request to Plausible failed after {_MAX_RETRIES} attempts: {exc}"
                    raise PlausibleAPIError(_redact_token(msg, self.api_key)) from exc

                sleep_time = 2**retries * 0.5
                time.sleep(sleep_time)

    def get_aggregate(
        self,
        site_id: str,
        period: str = "30d",
        metrics: str = "visitors,pageviews,bounce_rate,visit_duration,events",
    ) -> dict[str, Any]:
        """Fetch high-level aggregate metrics for a site."""
        params = {"site_id": site_id, "period": period, "metrics": metrics}
        return self._request("/aggregate", params=params)

    def get_breakdown(
        self,
        site_id: str,
        property_name: str,
        period: str = "30d",
        limit: int = 10,
    ) -> list[dict[str, Any]]:
        """Fetch breakdown stats by a specific property (e.g. event:page, visit:source)."""
        params = {
            "site_id": site_id,
            "period": period,
            "property": property_name,
            "limit": limit,
        }
        res = self._request("/breakdown", params=params)
        return res.get("results", [])


# ---------------------------------------------------------------------------
# Markdown Report Builders
# ---------------------------------------------------------------------------


def _build_overview_markdown(site_id: str, period: str, data: dict[str, Any]) -> str:
    """Format aggregate traffic metrics into structured Markdown."""
    res = data.get("results", {})
    visitors = res.get("visitors", {}).get("value", 0)
    pageviews = res.get("pageviews", {}).get("value", 0)
    bounce_rate = res.get("bounce_rate", {}).get("value", 0)
    visit_duration = res.get("visit_duration", {}).get("value", 0)
    events = res.get("events", {}).get("value", 0)

    # Format duration in minutes and seconds
    mins, secs = divmod(int(visit_duration), 60)
    duration_str = f"{mins}m {secs}s" if mins > 0 else f"{secs}s"

    return f"""# Plausible Analytics Overview: {site_id}

**Website Domain**: `{site_id}`
**Reporting Period**: `{period}`

## Traffic Summary
- **Unique Visitors**: {visitors:,}
- **Total Pageviews**: {pageviews:,}
- **Bounce Rate**: {bounce_rate}%
- **Average Visit Duration**: {duration_str}
- **Total Tracked Events**: {events:,}

This document represents aggregate traffic performance captured from Plausible Analytics.
"""


def _build_pages_markdown(site_id: str, period: str, pages: list[dict[str, Any]]) -> str:
    """Format top visited pages into structured Markdown."""
    lines = [
        f"# Plausible Top Pages & Content: {site_id}",
        f"\n**Website Domain**: `{site_id}`",
        f"**Reporting Period**: `{period}`\n",
        "## Top Visited Pages",
    ]

    if not pages:
        lines.append("\nNo pageview data recorded for this period.")
    else:
        lines.append("\n| Rank | Page Path | Visitors |")
        lines.append("| :--- | :--- | :--- |")
        for i, item in enumerate(pages, start=1):
            page_path = item.get("page", "/")
            visitors = item.get("visitors", 0)
            lines.append(f"| {i} | `{page_path}` | {visitors:,} |")

    return "\n".join(lines) + "\n"


def _build_sources_markdown(site_id: str, period: str, sources: list[dict[str, Any]]) -> str:
    """Format acquisition channels & referral sources into structured Markdown."""
    lines = [
        f"# Plausible Traffic Sources & Acquisition: {site_id}",
        f"\n**Website Domain**: `{site_id}`",
        f"**Reporting Period**: `{period}`\n",
        "## Top Referral Sources & Channels",
    ]

    if not sources:
        lines.append("\nNo referral source data recorded for this period.")
    else:
        lines.append("\n| Rank | Referral Source | Visitors |")
        lines.append("| :--- | :--- | :--- |")
        for i, item in enumerate(sources, start=1):
            source_name = item.get("source", "Direct / None")
            visitors = item.get("visitors", 0)
            lines.append(f"| {i} | {source_name} | {visitors:,} |")

    return "\n".join(lines) + "\n"


def _build_goals_markdown(site_id: str, period: str, goals: list[dict[str, Any]]) -> str:
    """Format goal conversions into structured Markdown."""
    lines = [
        f"# Plausible Goals & Conversions: {site_id}",
        f"\n**Website Domain**: `{site_id}`",
        f"**Reporting Period**: `{period}`\n",
        "## Completed Goals & Custom Events",
    ]

    if not goals:
        lines.append("\nNo custom events or goals tracked for this period.")
    else:
        lines.append("\n| Goal / Event Name | Total Events | Unique Visitors |")
        lines.append("| :--- | :--- | :--- |")
        for item in goals:
            goal_name = item.get("goal", "Unknown Goal")
            events = item.get("events", 0)
            visitors = item.get("visitors", 0)
            lines.append(f"| **{goal_name}** | {events:,} | {visitors:,} |")

    return "\n".join(lines) + "\n"


def _build_locations_markdown(
    site_id: str,
    period: str,
    countries: list[dict[str, Any]],
    devices: list[dict[str, Any]],
) -> str:
    """Format visitor demographics (countries & devices) into structured Markdown."""
    lines = [
        f"# Plausible Audience Demographics: {site_id}",
        f"\n**Website Domain**: `{site_id}`",
        f"**Reporting Period**: `{period}`\n",
        "## Geographic Distribution (Top Countries)",
    ]

    if not countries:
        lines.append("\nNo country data recorded.")
    else:
        lines.append("\n| Country | Visitors |")
        lines.append("| :--- | :--- |")
        for item in countries:
            country = item.get("country", "Unknown")
            visitors = item.get("visitors", 0)
            lines.append(f"| {country} | {visitors:,} |")

    lines.append("\n## Device Breakdown")
    if not devices:
        lines.append("\nNo device data recorded.")
    else:
        lines.append("\n| Device Type | Visitors |")
        lines.append("| :--- | :--- |")
        for item in devices:
            device = item.get("device", "Desktop")
            visitors = item.get("visitors", 0)
            lines.append(f"| {device} | {visitors:,} |")

    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# DLT Source Definition
# ---------------------------------------------------------------------------


def plausible_source(
    site_id: str | None = None,
    api_key: str | None = None,
    period: str = "30d",
    base_url: str = DEFAULT_BASE_URL,
    include_breakdowns: bool = True,
    include_goals: bool = True,
    client: PlausibleClient | None = None,
) -> Any:
    """Create a dlt source yielding Plausible Analytics reports as Markdown documents.

    Args:
        site_id: Domain name configured in Plausible (falls back to ``PLAUSIBLE_SITE_ID``).
        api_key: Plausible API token (falls back to ``PLAUSIBLE_API_KEY``).
        period: Time window to analyze (e.g. ``"30d"``, ``"7d"``, ``"month"``).
        base_url: Plausible API base URL (defaults to ``https://plausible.io``).
        include_breakdowns: Whether to fetch top pages, sources, and visitor demographics.
        include_goals: Whether to fetch custom events and conversion goals.
        client: Pre-built ``PlausibleClient`` instance (useful for testing/injection).

    Returns:
        A dlt source ready to pass to ``cognee.remember(...)`` or ``cognee.add(...)``.
    """
    try:
        import dlt
    except ImportError as exc:
        raise ImportError(_EXTRA_HINT) from exc

    resolved_site_id = site_id or os.environ.get("PLAUSIBLE_SITE_ID")
    if not resolved_site_id:
        raise ValueError(
            "site_id must be provided or set via PLAUSIBLE_SITE_ID environment variable."
        )

    resolved_key = api_key or os.environ.get("PLAUSIBLE_API_KEY")
    if not client and not resolved_key:
        raise ValueError(
            "api_key must be provided or set via PLAUSIBLE_API_KEY environment variable."
        )

    plausible_client = client or PlausibleClient(
        api_key=resolved_key or "",
        base_url=base_url,
    )

    @dlt.resource(name=PLAUSIBLE_TABLE_NAME, write_disposition="replace")
    def plausible_reports():
        logger.info("Fetching Plausible analytics for site=%s period=%s", resolved_site_id, period)

        # 1. High-level aggregate overview
        agg_data = plausible_client.get_aggregate(resolved_site_id, period=period)
        overview_md = _build_overview_markdown(resolved_site_id, period, agg_data)
        yield {
            "id": str(uuid5(NAMESPACE_OID, f"plausible:{resolved_site_id}:{period}:overview")),
            "title": f"Plausible Analytics Overview - {resolved_site_id} ({period})",
            "text": overview_md,
            "url": f"{plausible_client.base_url}/{resolved_site_id}",
            "metadata": {
                "source": PLAUSIBLE_SOURCE_NAME,
                "site_id": resolved_site_id,
                "period": period,
                "report_type": "overview",
            },
        }

        if include_breakdowns:
            # 2. Top pages
            pages = plausible_client.get_breakdown(
                resolved_site_id, property_name="event:page", period=period
            )
            pages_md = _build_pages_markdown(resolved_site_id, period, pages)
            yield {
                "id": str(uuid5(NAMESPACE_OID, f"plausible:{resolved_site_id}:{period}:pages")),
                "title": f"Plausible Top Pages - {resolved_site_id} ({period})",
                "text": pages_md,
                "url": f"{plausible_client.base_url}/{resolved_site_id}",
                "metadata": {
                    "source": PLAUSIBLE_SOURCE_NAME,
                    "site_id": resolved_site_id,
                    "period": period,
                    "report_type": "pages",
                },
            }

            # 3. Referral sources
            sources = plausible_client.get_breakdown(
                resolved_site_id, property_name="visit:source", period=period
            )
            sources_md = _build_sources_markdown(resolved_site_id, period, sources)
            yield {
                "id": str(uuid5(NAMESPACE_OID, f"plausible:{resolved_site_id}:{period}:sources")),
                "title": f"Plausible Referral Sources - {resolved_site_id} ({period})",
                "text": sources_md,
                "url": f"{plausible_client.base_url}/{resolved_site_id}",
                "metadata": {
                    "source": PLAUSIBLE_SOURCE_NAME,
                    "site_id": resolved_site_id,
                    "period": period,
                    "report_type": "sources",
                },
            }

            # 4. Audience Demographics (Countries & Devices)
            countries = plausible_client.get_breakdown(
                resolved_site_id, property_name="visit:country", period=period
            )
            devices = plausible_client.get_breakdown(
                resolved_site_id, property_name="visit:device", period=period
            )
            demographics_md = _build_locations_markdown(
                resolved_site_id, period, countries, devices
            )
            yield {
                "id": str(
                    uuid5(NAMESPACE_OID, f"plausible:{resolved_site_id}:{period}:demographics")
                ),
                "title": f"Plausible Audience Demographics - {resolved_site_id} ({period})",
                "text": demographics_md,
                "url": f"{plausible_client.base_url}/{resolved_site_id}",
                "metadata": {
                    "source": PLAUSIBLE_SOURCE_NAME,
                    "site_id": resolved_site_id,
                    "period": period,
                    "report_type": "demographics",
                },
            }

        if include_goals:
            # 5. Goals & Custom Events
            goals = plausible_client.get_breakdown(
                resolved_site_id, property_name="event:goal", period=period
            )
            goals_md = _build_goals_markdown(resolved_site_id, period, goals)
            yield {
                "id": str(uuid5(NAMESPACE_OID, f"plausible:{resolved_site_id}:{period}:goals")),
                "title": f"Plausible Goals & Conversions - {resolved_site_id} ({period})",
                "text": goals_md,
                "url": f"{plausible_client.base_url}/{resolved_site_id}",
                "metadata": {
                    "source": PLAUSIBLE_SOURCE_NAME,
                    "site_id": resolved_site_id,
                    "period": period,
                    "report_type": "goals",
                },
            }

    @dlt.source(name=PLAUSIBLE_SOURCE_NAME)
    def _source():
        return plausible_reports

    source_inst = _source()
    # Invariant: declare document source so cognee ingests through cognify entity extraction
    setattr(source_inst, DOCUMENT_SOURCE_ATTR, PLAUSIBLE_SOURCE_NAME)
    return source_inst
