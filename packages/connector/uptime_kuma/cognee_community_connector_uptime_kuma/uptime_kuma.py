"""DLT source for Uptime Kuma (infrastructure monitoring into AI knowledge graphs).

Fetches monitors, heartbeats, downtime incidents, and status-page announcements from
Uptime Kuma (self-hosted) and renders health telemetry into structured Markdown documents
for Cognee's ingestion and cognitive entity-extraction pipeline.

Like other core connectors, Uptime Kuma reports are ingested as *normal documents*:
the source declares ``cognee_document_source = "uptime_kuma"`` via ``DOCUMENT_SOURCE_ATTR``,
so ``resolve_dlt_sources`` tags each row with ``external_metadata["source"] = "uptime_kuma"``.
Reports flow through Cognee's standard ``cognify()`` pipeline, building a rich knowledge graph
of monitored endpoints, downtime events, error codes, and maintenance windows.

The source operates as a full snapshot: ``write_disposition="replace"`` writes the current
monitoring state. Stable deterministic IDs prevent duplicate re-cognification, while deleted
monitors or resolved maintenance posts drop out so Cognee's ``orphan_cleanup`` purges them.
"""

from __future__ import annotations

import os
import time
from typing import Any
from uuid import NAMESPACE_OID, uuid5

import httpx
from cognee.shared.logging_utils import get_logger
from cognee.tasks.ingestion.dlt_utils import DOCUMENT_SOURCE_ATTR

logger = get_logger("uptime_kuma_connector")

UPTIME_KUMA_SOURCE_NAME = "uptime_kuma"
UPTIME_KUMA_TABLE_NAME = "uptime_kuma_monitors"
DEFAULT_BASE_URL = "http://localhost:3001"

_MAX_RETRIES = 5
_DEFAULT_TIMEOUT = 30.0

_EXTRA_HINT = (
    "The Uptime Kuma connector requires dlt and httpx: "
    'pip install "cognee-community-connector-uptime-kuma"'
)


class UptimeKumaError(Exception):
    """Base exception for Uptime Kuma connector errors."""


class UptimeKumaAuthError(UptimeKumaError):
    """Raised when authentication fails (HTTP 401 or 403)."""


class UptimeKumaAPIError(UptimeKumaError):
    """Raised when Uptime Kuma API returns an error."""


class UptimeKumaRateLimitError(UptimeKumaError):
    """Raised when rate limits are exhausted after retries."""


def _redact_token(message: str, token: str | None) -> str:
    """Sanitize API keys and secrets from error messages and logs."""
    if not token or len(token) < 4:
        return message
    return message.replace(token, "[REDACTED_TOKEN]")


class UptimeKumaClient:
    """HTTP client for Uptime Kuma REST and Status Page endpoints."""

    def __init__(
        self,
        base_url: str = DEFAULT_BASE_URL,
        api_key: str | None = None,
        timeout: float = _DEFAULT_TIMEOUT,
        client: httpx.Client | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key.strip() if api_key else None
        self.timeout = timeout

        headers = {
            "User-Agent": "cognee-community-connector-uptime-kuma/0.1.0",
            "Accept": "application/json",
        }
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"

        self._client = client or httpx.Client(headers=headers, timeout=self.timeout)

    def _request(self, endpoint: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        """Execute HTTP GET request with retry backoff."""
        url = f"{self.base_url}{endpoint}"
        retries = 0

        while True:
            try:
                response = self._client.get(url, params=params)

                if response.status_code in (401, 403):
                    msg = (
                        f"Uptime Kuma authentication failed "
                        f"({response.status_code}): {response.text}"
                    )
                    raise UptimeKumaAuthError(_redact_token(msg, self.api_key))

                if response.status_code == 429:
                    retries += 1
                    if retries > _MAX_RETRIES:
                        raise UptimeKumaRateLimitError(
                            "Uptime Kuma rate limit exceeded after maximum retries."
                        )

                    retry_after = response.headers.get("Retry-After")
                    sleep_time = float(retry_after) if retry_after else (2**retries * 0.5)
                    logger.warning(
                        "Rate limited by Uptime Kuma (429). Retrying in %.2fs (attempt %d/%d)...",
                        sleep_time,
                        retries,
                        _MAX_RETRIES,
                    )
                    time.sleep(sleep_time)
                    continue

                if response.status_code >= 400:
                    msg = f"Uptime Kuma API error ({response.status_code}): {response.text}"
                    raise UptimeKumaAPIError(_redact_token(msg, self.api_key))

                return response.json()

            except (httpx.RequestError, httpx.HTTPStatusError) as exc:
                retries += 1
                if retries > _MAX_RETRIES:
                    msg = f"Request to Uptime Kuma failed after {_MAX_RETRIES} attempts: {exc}"
                    raise UptimeKumaAPIError(_redact_token(msg, self.api_key)) from exc

                sleep_time = 2**retries * 0.5
                time.sleep(sleep_time)

    def get_status_page(self, slug: str = "default") -> dict[str, Any]:
        """Fetch status page monitor groups and maintenance posts."""
        return self._request(f"/api/status-page/{slug}")

    def get_heartbeats(self, slug: str = "default") -> dict[str, Any]:
        """Fetch recent heartbeat logs and latencies for status page monitors."""
        return self._request(f"/api/status-page/heartbeat/{slug}")

    def get_monitors(self) -> list[dict[str, Any]]:
        """Fetch monitor list from authenticated instance."""
        res = self._request("/api/monitor")
        return res if isinstance(res, list) else res.get("monitors", [])


# ---------------------------------------------------------------------------
# Markdown Formatting Helpers
# ---------------------------------------------------------------------------


def _status_str(status: int | str) -> str:
    """Format status numeric flag into readable string."""
    if status == 1 or status == "1" or status == "UP":
        return "UP (Healthy)"
    if status == 0 or status == "0" or status == "DOWN":
        return "DOWN (Outage)"
    if status == 2 or status == "2" or status == "PENDING":
        return "PENDING"
    if status == 3 or status == "3" or status == "MAINTENANCE":
        return "UNDER MAINTENANCE"
    return str(status)


def _build_monitors_markdown(
    base_url: str,
    slug: str,
    monitors: list[dict[str, Any]],
    heartbeats: dict[str, Any] | None = None,
) -> str:
    """Format monitor health and endpoints into structured Markdown."""
    lines = [
        f"# Uptime Kuma Monitors: {slug}",
        f"\n**Server**: `{base_url}`",
        f"**Status Page Slug**: `{slug}`\n",
        "## Active Service Monitors",
    ]

    if not monitors:
        lines.append("\nNo monitors found on this status page.")
        return "\n".join(lines) + "\n"

    lines.append("\n| ID | Monitor Name | Type | Current Status | Target / URL | Latency |")
    lines.append("| :--- | :--- | :--- | :--- | :--- | :--- |")

    hb_dict = heartbeats.get("heartbeatList", {}) if heartbeats else {}

    for m in monitors:
        m_id = str(m.get("id", ""))
        name = m.get("name", "Unnamed Monitor")
        m_type = m.get("type", "HTTP").upper()
        target = m.get("url") or m.get("hostname") or "-"

        # Get latest ping latency from heartbeats
        m_heartbeats = hb_dict.get(m_id, [])
        latest_hb = m_heartbeats[-1] if m_heartbeats else {}
        status = _status_str(latest_hb.get("status", m.get("status", 1)))
        ping = latest_hb.get("ping")
        ping_str = f"{ping}ms" if ping is not None else "-"

        lines.append(f"| {m_id} | **{name}** | `{m_type}` | {status} | `{target}` | {ping_str} |")

    return "\n".join(lines) + "\n"


def _build_incidents_markdown(
    slug: str,
    monitors: list[dict[str, Any]],
    heartbeats: dict[str, Any],
) -> str:
    """Format recent downtime events and error codes into structured Markdown."""
    lines = [
        f"# Uptime Kuma Outages & Incident History: {slug}",
        f"\n**Status Page Slug**: `{slug}`\n",
        "## Recent Downtime Incidents",
    ]

    hb_dict = heartbeats.get("heartbeatList", {}) if heartbeats else {}
    monitor_names = {str(m.get("id", "")): m.get("name", "Unknown") for m in monitors}

    incident_count = 0
    lines.append("\n| Monitor Name | Outage Time | Status Code / Error Message | Duration |")
    lines.append("| :--- | :--- | :--- | :--- |")

    for m_id, hb_list in hb_dict.items():
        m_name = monitor_names.get(str(m_id), f"Monitor {m_id}")
        for hb in hb_list:
            if hb.get("status") == 0:  # 0 indicates DOWN
                incident_count += 1
                hb_time = hb.get("time", "Unknown time")
                msg = hb.get("msg") or "Connection failure / timeout"
                duration = hb.get("duration")
                dur_str = f"{duration}s" if duration else "Transient"
                lines.append(f"| **{m_name}** | {hb_time} | `{msg}` | {dur_str} |")

    if incident_count == 0:
        lines = [
            f"# Uptime Kuma Outages & Incident History: {slug}",
            f"\n**Status Page Slug**: `{slug}`\n",
            "## Incident Summary",
            (
                "\nAll monitored services operational. "
                "Zero outages recorded in recent heartbeat history."
            ),
        ]

    return "\n".join(lines) + "\n"


def _build_status_page_markdown(slug: str, data: dict[str, Any]) -> str:
    """Format status page announcements, maintenance posts, and group layout."""
    title = data.get("title", f"Status Page - {slug}")
    description = data.get("description", "System health and status board.")
    incident = data.get("incident")

    lines = [
        f"# Status Board: {title}",
        f"\n**Slug**: `{slug}`",
        f"**Description**: {description}\n",
    ]

    if incident:
        inc_title = incident.get("title", "Active Incident")
        inc_content = incident.get("content", "Investigating service degradation.")
        inc_style = incident.get("style", "warning").upper()
        inc_created = incident.get("createdDate", "")
        lines.append("## Active Incident Announcement")
        lines.append(f"- **Title**: {inc_title} ({inc_style})")
        lines.append(f"- **Posted At**: {inc_created}")
        lines.append(f"- **Details**: {inc_content}\n")
    else:
        lines.append("## Current Status Announcement")
        lines.append(
            "\nNo active maintenance windows or incident announcements currently posted.\n"
        )

    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# DLT Source Definition
# ---------------------------------------------------------------------------


def uptime_kuma_source(
    base_url: str = DEFAULT_BASE_URL,
    api_key: str | None = None,
    slug: str = "default",
    include_heartbeats: bool = True,
    include_incidents: bool = True,
    client: UptimeKumaClient | None = None,
) -> Any:
    """Create a dlt source yielding Uptime Kuma health data as Markdown documents.

    Args:
        base_url: Base URL of the Uptime Kuma instance.
        api_key: Optional API key for authenticated endpoints.
        slug: Status page slug (e.g. ``"default"`` or ``"main"``).
        include_heartbeats: Whether to fetch latency metrics and heartbeat logs.
        include_incidents: Whether to format downtime incidents and maintenance announcements.
        client: Pre-built ``UptimeKumaClient`` instance (useful for testing/injection).

    Returns:
        A dlt source ready to pass to ``cognee.remember(...)`` or ``cognee.add(...)``.
    """
    try:
        import dlt
    except ImportError as exc:
        raise ImportError(_EXTRA_HINT) from exc

    resolved_url = base_url or os.environ.get("UPTIME_KUMA_BASE_URL", DEFAULT_BASE_URL)
    resolved_key = api_key or os.environ.get("UPTIME_KUMA_API_KEY")

    kuma_client = client or UptimeKumaClient(
        base_url=resolved_url,
        api_key=resolved_key,
    )

    @dlt.resource(name=UPTIME_KUMA_TABLE_NAME, write_disposition="replace")
    def uptime_kuma_reports():
        logger.info("Fetching Uptime Kuma status for slug=%s at url=%s", slug, resolved_url)

        # 1. Fetch Status Page info and monitor list
        sp_data = kuma_client.get_status_page(slug)
        monitors: list[dict[str, Any]] = []

        # Extract monitors from publicGroupList
        for group in sp_data.get("publicGroupList", []):
            group_monitors = group.get("monitorList", [])
            monitors.extend(group_monitors)

        heartbeats_data = None
        if include_heartbeats:
            heartbeats_data = kuma_client.get_heartbeats(slug)

        # 2. Yield Monitors Health Overview
        monitors_md = _build_monitors_markdown(resolved_url, slug, monitors, heartbeats_data)
        yield {
            "id": str(uuid5(NAMESPACE_OID, f"uptime_kuma:{slug}:monitors")),
            "title": f"Uptime Kuma Service Monitors - {slug}",
            "text": monitors_md,
            "url": f"{resolved_url}/status/{slug}",
            "metadata": {
                "source": UPTIME_KUMA_SOURCE_NAME,
                "slug": slug,
                "report_type": "monitors",
            },
        }

        # 3. Yield Status Page Announcement Overview
        status_board_md = _build_status_page_markdown(slug, sp_data)
        yield {
            "id": str(uuid5(NAMESPACE_OID, f"uptime_kuma:{slug}:status_board")),
            "title": f"Uptime Kuma Status Board - {slug}",
            "text": status_board_md,
            "url": f"{resolved_url}/status/{slug}",
            "metadata": {
                "source": UPTIME_KUMA_SOURCE_NAME,
                "slug": slug,
                "report_type": "status_board",
            },
        }

        # 4. Yield Outage & Incident History if heartbeats are fetched
        if include_incidents and heartbeats_data:
            incidents_md = _build_incidents_markdown(slug, monitors, heartbeats_data)
            yield {
                "id": str(uuid5(NAMESPACE_OID, f"uptime_kuma:{slug}:incidents")),
                "title": f"Uptime Kuma Outages & Incident History - {slug}",
                "text": incidents_md,
                "url": f"{resolved_url}/status/{slug}",
                "metadata": {
                    "source": UPTIME_KUMA_SOURCE_NAME,
                    "slug": slug,
                    "report_type": "incidents",
                },
            }

    @dlt.source(name=UPTIME_KUMA_SOURCE_NAME)
    def _source():
        return uptime_kuma_reports

    source_inst = _source()
    setattr(source_inst, DOCUMENT_SOURCE_ATTR, UPTIME_KUMA_SOURCE_NAME)
    return source_inst
