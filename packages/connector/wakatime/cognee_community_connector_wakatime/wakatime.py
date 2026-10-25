"""DLT source for WakaTime developer activity and metrics (snapshot sync + forget-on-delete).

Fetches daily coding summaries, project durations, and language/editor metrics from WakaTime,
aggregates daily productivity reports into markdown documents, and yields them as a dlt
resource for Cognee's AI knowledge graph.
"""

import base64
from collections.abc import Iterator
from datetime import datetime, timedelta, timezone
import os
import time
from typing import Any

try:
    from cognee.shared.logging_utils import get_logger
    logger = get_logger("wakatime_connector")
except ImportError:
    import logging
    logger = logging.getLogger("wakatime_connector")

try:
    from cognee.tasks.ingestion.dlt_utils import DOCUMENT_SOURCE_ATTR
except ImportError:
    DOCUMENT_SOURCE_ATTR = "cognee_document_source"

WAKATIME_TABLE_NAME = "wakatime_activities"
WAKATIME_SOURCE_NAME = "wakatime"
WAKATIME_API_BASE = "https://wakatime.com/api/v1"

_MAX_RETRIES = 5
_DEFAULT_DAYS = 14


class WakaTimeClient:
    """Client for the WakaTime v1 REST API using Basic Auth."""

    def __init__(
        self,
        api_key: str | None = None,
        base_url: str = WAKATIME_API_BASE,
        http_client: Any = None,
    ):
        import httpx

        self.api_key = api_key or os.environ.get("WAKATIME_API_KEY")
        if not self.api_key and http_client is None:
            raise ValueError(
                "WakaTime API key required: pass api_key= or set WAKATIME_API_KEY environment variable."
            )

        self.base_url = base_url.rstrip("/")

        # Format HTTP Basic Auth header: Authorization: Basic <base64(api_key)>
        headers = {}
        if self.api_key:
            encoded_key = base64.b64encode(self.api_key.encode("utf-8")).decode("utf-8")
            headers["Authorization"] = f"Basic {encoded_key}"

        self.client = http_client or httpx.Client(
            headers=headers,
            timeout=25.0,
        )

    def get_summaries(self, start_date: str, end_date: str, project: str | None = None) -> list[dict[str, Any]]:
        """Fetch daily summaries within the date range [start_date, end_date] (YYYY-MM-DD)."""
        params: dict[str, Any] = {"start": start_date, "end": end_date}
        if project:
            params["project"] = project

        endpoint = f"{self.base_url}/users/current/summaries"
        data = self._request(endpoint, params=params)
        return data.get("data", [])

    def get_user_stats(self, range_name: str = "last_7_days") -> dict[str, Any]:
        """Fetch aggregate developer stats (languages, editors, projects)."""
        endpoint = f"{self.base_url}/users/current/stats/{range_name}"
        data = self._request(endpoint)
        return data.get("data", {})

    def _request(self, url: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        import httpx

        for attempt in range(_MAX_RETRIES):
            try:
                resp = self.client.get(url, params=params)
                if resp.status_code == 429:
                    delay = float(resp.headers.get("Retry-After", 2 ** attempt))
                    time.sleep(delay)
                    continue
                resp.raise_for_status()
                return resp.json()
            except (httpx.TransportError, httpx.HTTPStatusError) as exc:
                if attempt == _MAX_RETRIES - 1:
                    logger.warning("WakaTime API request failed for %s: %s", url, exc)
                    raise
                time.sleep(2 ** attempt)

        return {}


def wakatime_source(
    api_key: str | None = None,
    days: int = _DEFAULT_DAYS,
    start_date: str | None = None,
    end_date: str | None = None,
    projects: list[str] | None = None,
    client: WakaTimeClient | None = None,
):
    """Create a dlt source yielding WakaTime coding summaries as markdown documents.

    Args:
        api_key: WakaTime secret API key. Falls back to WAKATIME_API_KEY env var.
        days: Number of recent days to sync (default: 14 days, tailored for free plan limits).
        start_date: Optional explicit start date (YYYY-MM-DD).
        end_date: Optional explicit end date (YYYY-MM-DD). Defaults to today.
        projects: Optional list of project names to restrict activity reports.
        client: Pre-built WakaTimeClient instance (useful for hermetic unit testing).

    Returns:
        A dlt source yielding documents ready for Cognee ingestion.
    """
    import dlt

    if client is None:
        client = WakaTimeClient(api_key=api_key)

    # Calculate date range
    today = datetime.now(timezone.utc).date()
    resolved_end = end_date or today.strftime("%Y-%m-%d")
    resolved_start = start_date or (today - timedelta(days=days)).strftime("%Y-%m-%d")

    target_projects = projects or [None]

    @dlt.resource(name=WAKATIME_TABLE_NAME, primary_key="id", write_disposition="replace")
    def wakatime_activities() -> Iterator[dict[str, Any]]:
        seen_ids = set()
        count = 0

        for prj in target_projects:
            try:
                daily_summaries = client.get_summaries(resolved_start, resolved_end, project=prj)
                for day_data in daily_summaries:
                    row = _summary_to_row(day_data, specific_project=prj)
                    if row and row["id"] not in seen_ids:
                        seen_ids.add(row["id"])
                        count += 1
                        yield row
            except Exception as exc:
                logger.warning("Error syncing WakaTime summaries for project %s: %s", prj, exc)

        # Ingest overall weekly productivity overview
        try:
            stats_data = client.get_user_stats(range_name="last_7_days")
            overview_row = _stats_to_row(stats_data)
            if overview_row and overview_row["id"] not in seen_ids:
                seen_ids.add(overview_row["id"])
                count += 1
                yield overview_row
        except Exception as exc:
            logger.debug("Aggregate stats ingestion skipped: %s", exc)

        logger.info("WakaTime connector: synced %d activity report(s).", count)

    @dlt.source(name=WAKATIME_SOURCE_NAME)
    def _wakatime():
        return wakatime_activities

    source = _wakatime()
    setattr(source, DOCUMENT_SOURCE_ATTR, WAKATIME_SOURCE_NAME)
    return source


def _summary_to_row(day_data: dict[str, Any], specific_project: str | None = None) -> dict[str, Any] | None:
    """Format a single day's summary into a structured markdown document row."""
    date_str = day_data.get("range", {}).get("date")
    if not date_str:
        return None

    grand_total = day_data.get("grand_total", {}).get("text", "0 secs")
    total_seconds = day_data.get("grand_total", {}).get("total_seconds", 0)

    title_prefix = f"Project: {specific_project} — " if specific_project else ""
    title = f"WakaTime Coding Activity: {title_prefix}{date_str} ({grand_total})"

    md_lines = [
        f"# {title}",
        f"**Date:** {date_str} | **Total Active Coding Time:** {grand_total} ({total_seconds:.0f}s)",
        "",
        "## Projects Breakdown",
    ]

    projects = day_data.get("projects", [])
    if projects:
        for p in projects:
            p_name = p.get("name", "Unknown")
            p_text = p.get("text", "")
            p_pct = p.get("percent", 0.0)
            md_lines.append(f"- **{p_name}**: {p_text} ({p_pct:.1f}%)")
    else:
        md_lines.append("- No project activity recorded.")

    md_lines.extend(["", "## Languages Used"])
    languages = day_data.get("languages", [])
    if languages:
        for lang in languages:
            l_name = lang.get("name", "Unknown")
            l_text = lang.get("text", "")
            l_pct = lang.get("percent", 0.0)
            md_lines.append(f"- **{l_name}**: {l_text} ({l_pct:.1f}%)")
    else:
        md_lines.append("- No language telemetry recorded.")

    md_lines.extend(["", "## Editors & Environments"])
    editors = day_data.get("editors", [])
    if editors:
        for ed in editors:
            e_name = ed.get("name", "Unknown")
            e_text = ed.get("text", "")
            md_lines.append(f"- **{e_name}**: {e_text}")

    doc_id = f"wakatime_{date_str}_{specific_project or 'all'}"
    canonical_url = f"https://wakatime.com/dashboard/day?date={date_str}"

    return {
        "id": doc_id,
        "url": canonical_url,
        "title": title,
        "content": "\n".join(md_lines),
    }


def _stats_to_row(stats_data: dict[str, Any]) -> dict[str, Any] | None:
    """Format aggregate developer profile stats into an overview document."""
    if not stats_data:
        return None

    range_text = stats_data.get("human_readable_range", "Last 7 Days")
    total_time = stats_data.get("human_readable_total", "0 hrs")
    daily_avg = stats_data.get("human_readable_daily_average", "0 hrs")

    title = f"WakaTime Developer Productivity Overview ({range_text})"
    md_lines = [
        f"# {title}",
        f"**Summary Period:** {range_text}",
        f"**Total Tracked Time:** {total_time} | **Daily Average:** {daily_avg}",
        "",
        "## Top Projects",
    ]

    for p in stats_data.get("projects", [])[:8]:
        md_lines.append(f"- **{p.get('name')}**: {p.get('text')} ({p.get('percent', 0.0):.1f}%)")

    md_lines.extend(["", "## Top Languages"])
    for l_entry in stats_data.get("languages", [])[:8]:
        md_lines.append(f"- **{l_entry.get('name')}**: {l_entry.get('text')} ({l_entry.get('percent', 0.0):.1f}%)")

    md_lines.extend(["", "## Primary Editors"])
    for ed in stats_data.get("editors", [])[:5]:
        md_lines.append(f"- **{ed.get('name')}**: {ed.get('text')} ({ed.get('percent', 0.0):.1f}%)")

    return {
        "id": "wakatime_productivity_overview_7d",
        "url": "https://wakatime.com/dashboard",
        "title": title,
        "content": "\n".join(md_lines),
    }
