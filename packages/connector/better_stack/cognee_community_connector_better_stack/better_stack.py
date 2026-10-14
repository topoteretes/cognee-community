"""DLT source for Better Stack / Better Uptime (monitors, incidents, timelines).

Fetches uptime monitors, incident reports, and chronological outage timelines from
Better Stack (https://betterstack.com/uptime) and yields them as structured markdown
documents for Cognee's AI agent memory graph.

The source declares ``cognee_document_source = "better_stack"``, routing each record
through the standard cognify entity-extraction pipeline.
"""

import os
import time
from typing import Any, Dict, Generator, List, Optional
import urllib.request
import urllib.error
import json

try:
    from cognee.shared.logging_utils import get_logger
    logger = get_logger("better_stack_connector")
except ImportError:
    import logging
    logger = logging.getLogger("better_stack_connector")

try:
    from cognee.tasks.ingestion.dlt_utils import DOCUMENT_SOURCE_ATTR
except ImportError:
    DOCUMENT_SOURCE_ATTR = "cognee_document_source"

BETTER_STACK_TABLE_NAME = "better_stack_incidents"
BETTER_STACK_SOURCE_NAME = "better_stack"
BETTER_STACK_API_BASE = "https://uptime.betterstack.com/api/v2"

_MAX_RETRIES = 5


def better_stack_source(
    api_token: Optional[str] = None,
    client: Any = None,
):
    """Create a dlt source that yields Better Stack incidents & monitors as markdown documents.

    Args:
        api_token: Better Stack API Bearer token. Falls back to ``BETTER_STACK_API_TOKEN``.
        client: Pre-built HTTP client / test-injection point; when omitted, uses internal REST requester.

    Returns:
        A dlt source suitable for ``cognee.add(...)``.
    """
    try:
        import dlt
    except ImportError as exc:
        raise ImportError(
            "The Better Stack connector requires dlt: pip install 'dlt[sqlalchemy]>=1.9.0,<2'"
        ) from exc

    resolved_token = api_token or os.environ.get("BETTER_STACK_API_TOKEN")
    if client is None and not resolved_token:
        raise ValueError(
            "Better Stack API token required: pass api_token= or set BETTER_STACK_API_TOKEN."
        )

    requester = client or BetterStackClient(api_token=resolved_token)

    @dlt.resource(name=BETTER_STACK_TABLE_NAME, primary_key="id", write_disposition="replace")
    def better_stack_incidents():
        count = 0
        for incident in requester.iter_incidents():
            count += 1
            yield _incident_to_row(requester, incident)
        logger.info("Better Stack: synced %d incident(s).", count)

    @dlt.source(name=BETTER_STACK_SOURCE_NAME)
    def _better_stack():
        return better_stack_incidents

    source = _better_stack()
    setattr(source, DOCUMENT_SOURCE_ATTR, BETTER_STACK_SOURCE_NAME)
    return source


# ---------------------------------------------------------------------------
# API Client & Helpers
# ---------------------------------------------------------------------------


class BetterStackClient:
    """REST API Client for Better Stack Uptime API v2."""

    def __init__(self, api_token: str, base_url: str = BETTER_STACK_API_BASE):
        self.api_token = api_token
        self.base_url = base_url.rstrip("/")

    def _request(self, endpoint: str, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        url = f"{self.base_url}{endpoint}"
        if params:
            query_str = urllib.parse.urlencode({k: v for k, v in params.items() if v is not None})
            if query_str:
                url = f"{url}?{query_str}"

        headers = {
            "Authorization": f"Bearer {self.api_token}",
            "User-Agent": "cognee-community-connector-better-stack",
            "Accept": "application/json",
        }

        req = urllib.request.Request(url, headers=headers, method="GET")

        for attempt in range(_MAX_RETRIES):
            try:
                with urllib.request.urlopen(req) as resp:
                    return json.loads(resp.read().decode("utf-8"))
            except urllib.error.HTTPError as exc:
                if attempt == _MAX_RETRIES - 1 or exc.code not in (429, 500, 502, 503, 504):
                    raise
                retry_after = exc.headers.get("Retry-After")
                delay = float(retry_after) if retry_after else float(2**attempt)
                logger.warning("Better Stack: HTTP %d, retrying in %.1fs...", exc.code, delay)
                time.sleep(delay)
            except urllib.error.URLError as exc:
                if attempt == _MAX_RETRIES - 1:
                    raise
                time.sleep(float(2**attempt))
        return {}

    def iter_incidents(self) -> Generator[Dict[str, Any], None, None]:
        page = 1
        while True:
            data = self._request("/incidents", params={"page": page})
            incidents = data.get("data", [])
            for inc in incidents:
                yield inc

            pagination = data.get("pagination", {})
            next_page = pagination.get("next")
            if not next_page or not incidents:
                break
            page += 1

    def get_incident_timeline(self, incident_id: str) -> List[Dict[str, Any]]:
        """Fetch timeline updates / events for an incident."""
        try:
            data = self._request(f"/incidents/{incident_id}/timeline")
            return data.get("data", [])
        except Exception as exc:
            logger.warning("Could not fetch timeline for incident %s: %s", incident_id, exc)
            return []


# ---------------------------------------------------------------------------
# Document formatting & Markdown Rendering
# ---------------------------------------------------------------------------


def _incident_to_row(client: Any, item: Dict[str, Any]) -> Dict[str, Any]:
    """Convert a Better Stack incident item into a document for Cognee memory."""
    incident_id = item.get("id", "")
    attributes = item.get("attributes", {})
    name = attributes.get("name", f"Incident #{incident_id}")
    url = attributes.get("url") or f"https://uptime.betterstack.com/incidents/{incident_id}"

    timeline = client.get_incident_timeline(incident_id) if hasattr(client, "get_incident_timeline") else []

    content = _render_better_stack_markdown(attributes, timeline)

    return {
        "id": incident_id,
        "url": url,
        "title": f"Better Stack Outage: {name}",
        "content": content,
    }


def _render_better_stack_markdown(
    attributes: Dict[str, Any],
    timeline: List[Dict[str, Any]],
) -> str:
    """Render Better Stack outage attributes and timeline events into markdown."""
    name = attributes.get("name", "Outage")
    cause = attributes.get("cause") or "Root cause not specified."
    status = attributes.get("status", "unknown")
    started_at = attributes.get("started_at", "unknown")
    resolved_at = attributes.get("resolved_at") or "Ongoing"
    response_content = attributes.get("response_content") or ""

    lines = [
        f"# Better Stack Incident: {name}",
        "",
        f"- **Status:** {status}",
        f"- **Started At:** {started_at}",
        f"- **Resolved At:** {resolved_at}",
        "",
        "## Root Cause & Description",
        cause,
        "",
    ]

    if response_content:
        lines.append("## Response Content")
        lines.append(f"```\n{response_content}\n```")
        lines.append("")

    if timeline:
        lines.append("## Chronological Timeline")
        for event in timeline:
            ev_attrs = event.get("attributes", {})
            occurred_at = ev_attrs.get("occurred_at", "")
            action = ev_attrs.get("action", "").title()
            comment = ev_attrs.get("comment") or ""
            detail = f" - {comment}" if comment else ""
            lines.append(f"- **[{occurred_at}]** {action}{detail}")
        lines.append("")

    return "\n".join(lines)
