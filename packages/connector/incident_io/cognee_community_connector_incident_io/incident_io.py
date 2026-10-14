"""DLT source for incident.io (incidents, updates, actions + forget-on-delete).

Fetches incidents, post-mortem timelines, and remediation action items from incident.io
and renders them into structured markdown documents for Cognee's memory graph.

The source declares ``cognee_document_source = "incident_io"``, so ``resolve_dlt_sources``
routes each incident document through the standard cognify entity-extraction pipeline
(prose document path) instead of the relational dlt-row schema-context path.

Supports full snapshot / replace sync: incidents deleted or purged upstream drop out
of listings and are pruned from the graph and vector stores.
"""

import os
import time
from typing import Any, Callable, Dict, Generator, List, Optional
import urllib.request
import urllib.error
import json

try:
    from cognee.shared.logging_utils import get_logger
    logger = get_logger("incident_io_connector")
except ImportError:
    import logging
    logger = logging.getLogger("incident_io_connector")

try:
    from cognee.tasks.ingestion.dlt_utils import DOCUMENT_SOURCE_ATTR
except ImportError:
    DOCUMENT_SOURCE_ATTR = "cognee_document_source"

INCIDENT_IO_TABLE_NAME = "incident_io_incidents"
INCIDENT_IO_SOURCE_NAME = "incident_io"
INCIDENT_IO_API_BASE = "https://api.incident.io/v2"

_MAX_RETRIES = 5


def incident_io_source(
    api_key: Optional[str] = None,
    status: Optional[List[str]] = None,
    client: Any = None,
):
    """Create a dlt source that yields incident.io records as markdown documents.

    Args:
        api_key: incident.io API key (Bearer token). Falls back to ``INCIDENT_IO_API_KEY``.
        status: Optional list of incident statuses to filter (e.g. ['triage', 'investigating', 'resolved']).
        client: Pre-built HTTP client / test-injection point; when omitted, uses internal REST requester.

    Returns:
        A dlt source suitable for ``cognee.add(...)``.
    """
    try:
        import dlt
    except ImportError as exc:
        raise ImportError(
            "The incident.io connector requires dlt: pip install 'dlt[sqlalchemy]>=1.9.0,<2'"
        ) from exc

    resolved_api_key = api_key or os.environ.get("INCIDENT_IO_API_KEY")
    if client is None and not resolved_api_key:
        raise ValueError(
            "incident.io API key required: pass api_key= or set INCIDENT_IO_API_KEY."
        )

    requester = client or IncidentIOClient(api_key=resolved_api_key)

    @dlt.resource(name=INCIDENT_IO_TABLE_NAME, primary_key="id", write_disposition="replace")
    def incident_io_incidents():
        count = 0
        for incident in requester.iter_incidents(status=status):
            count += 1
            yield _incident_to_row(requester, incident)
        logger.info("incident.io: synced %d incident(s).", count)

    @dlt.source(name=INCIDENT_IO_SOURCE_NAME)
    def _incident_io():
        return incident_io_incidents

    source = _incident_io()
    setattr(source, DOCUMENT_SOURCE_ATTR, INCIDENT_IO_SOURCE_NAME)
    return source


# ---------------------------------------------------------------------------
# API Client & Helpers
# ---------------------------------------------------------------------------


class IncidentIOClient:
    """REST API Client for incident.io v2 with rate limit backoff and pagination."""

    def __init__(self, api_key: str, base_url: str = INCIDENT_IO_API_BASE):
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")

    def _request(self, endpoint: str, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        url = f"{self.base_url}{endpoint}"
        if params:
            query_str = urllib.parse.urlencode({k: v for k, v in params.items() if v is not None})
            if query_str:
                url = f"{url}?{query_str}"

        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "User-Agent": "cognee-community-connector-incident-io",
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
                logger.warning("incident.io: HTTP %d, retrying in %.1fs...", exc.code, delay)
                time.sleep(delay)
            except urllib.error.URLError as exc:
                if attempt == _MAX_RETRIES - 1:
                    raise
                time.sleep(float(2**attempt))
        return {}

    def iter_incidents(self, status: Optional[List[str]] = None) -> Generator[Dict[str, Any], None, None]:
        after = None
        while True:
            params: Dict[str, Any] = {"page_size": 100}
            if after:
                params["after"] = after
            if status:
                params["status"] = status

            data = self._request("/incidents", params=params)
            incidents = data.get("incidents", [])
            for inc in incidents:
                yield inc

            pagination = data.get("pagination_meta", {})
            after = pagination.get("after")
            if not after or not incidents:
                break

    def get_incident_updates(self, incident_id: str) -> List[Dict[str, Any]]:
        """Fetch timeline updates for an incident."""
        try:
            data = self._request("/incident_updates", params={"incident_id": incident_id})
            return data.get("incident_updates", [])
        except Exception as exc:
            logger.warning("Could not fetch updates for incident %s: %s", incident_id, exc)
            return []

    def get_incident_actions(self, incident_id: str) -> List[Dict[str, Any]]:
        """Fetch follow-up action items for an incident."""
        try:
            data = self._request("/incident_actions", params={"incident_id": incident_id})
            return data.get("incident_actions", [])
        except Exception as exc:
            logger.warning("Could not fetch actions for incident %s: %s", incident_id, exc)
            return []


# ---------------------------------------------------------------------------
# Document formatting & Markdown Rendering
# ---------------------------------------------------------------------------


def _incident_to_row(client: Any, incident: Dict[str, Any]) -> Dict[str, Any]:
    """Convert an incident.io incident record into a document for Cognee memory."""
    incident_id = incident.get("id", "")
    incident_name = incident.get("name", "Untitled Incident")
    permalink = incident.get("permalink", f"https://app.incident.io/incidents/{incident_id}")

    # Sub-resources
    updates = client.get_incident_updates(incident_id) if hasattr(client, "get_incident_updates") else []
    actions = client.get_incident_actions(incident_id) if hasattr(client, "get_incident_actions") else []

    content = _render_incident_markdown(incident, updates, actions)

    return {
        "id": incident_id,
        "url": permalink,
        "title": f"Incident: {incident_name}",
        "content": content,
    }


def _render_incident_markdown(
    incident: Dict[str, Any],
    updates: List[Dict[str, Any]],
    actions: List[Dict[str, Any]],
) -> str:
    """Render incident details, severity, components, timeline, and actions to markdown."""
    name = incident.get("name", "Untitled Incident")
    summary = incident.get("summary") or "No summary provided."
    status = incident.get("incident_status", {}).get("name") if isinstance(incident.get("incident_status"), dict) else incident.get("status", "unknown")
    severity = incident.get("severity", {}).get("name") if isinstance(incident.get("severity"), dict) else "unknown"
    created_at = incident.get("created_at", "unknown")
    resolved_at = incident.get("resolved_at") or "Ongoing"

    # Lead / Owner
    lead_name = "Unassigned"
    creator = incident.get("creator", {})
    if isinstance(creator, dict) and creator.get("name"):
        lead_name = creator.get("name")

    lines = [
        f"# Incident: {name}",
        "",
        f"- **Status:** {status}",
        f"- **Severity:** {severity}",
        f"- **Lead:** {lead_name}",
        f"- **Opened:** {created_at}",
        f"- **Resolved:** {resolved_at}",
        "",
        "## Summary",
        summary,
        "",
    ]

    # Timeline updates
    if updates:
        lines.append("## Timeline & Milestone Updates")
        for upd in sorted(updates, key=lambda u: u.get("created_at", "")):
            ts = upd.get("created_at", "")
            msg = upd.get("message", "").strip()
            lines.append(f"- **[{ts}]** {msg}")
        lines.append("")

    # Action items
    if actions:
        lines.append("## Remediation & Follow-up Actions")
        for act in actions:
            act_desc = act.get("description", "").strip()
            act_status = act.get("status", "outstanding")
            completed = "x" if act_status == "completed" else " "
            assignee = act.get("assignee", {}).get("name") if isinstance(act.get("assignee"), dict) else "Unassigned"
            lines.append(f"- [{completed}] {act_desc} (Assignee: {assignee})")
        lines.append("")

    return "\n".join(lines)
