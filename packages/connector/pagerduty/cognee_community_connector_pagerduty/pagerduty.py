"""DLT source for PagerDuty (incidents, responders, alerts + forget-on-delete).

Syncs on-call incident reports, responder assignments, and operational alerts
from PagerDuty (https://pagerduty.com) into Cognee's AI-agent memory graph.

Each incident becomes a prose document that the standard cognify pipeline
processes into an entity/relationship graph, so agents can answer questions
like "Who was the incident commander for the checkout-service outage?" or
"What high-urgency alerts triggered during the database failover?"

Design
------
* **Auth** — API token via the PagerDuty REST API v2. Pass ``api_token``
  or set ``PAGERDUTY_API_TOKEN``. A missing token raises a clear ``ValueError``
  at call time.
* **Header format** — Uses ``Authorization: Token token={api_token}`` and
  ``Accept: application/vnd.pagerduty+json;version=2`` per PagerDuty v2 spec.

* **Snapshot with forget-on-delete** — ``write_disposition="replace"`` gives
  cognee a complete view of every incident on each run. Incidents resolved or
  purged drop out of the listing so cognee's ``orphan_cleanup`` removes their
  nodes and vectors automatically.

* **Pagination** — PagerDuty uses offset pagination (``limit=100&offset=N``).
  Iterates until ``more`` is ``False`` or no more items are returned.

* **Retry / rate-limit** — HTTP 429 and 5xx responses are retried with
  exponential backoff up to ``_MAX_RETRIES`` times, honouring the
  ``Retry-After`` header when present.

* **Document path** — ``cognee_document_source = "pagerduty"`` tags the
  source so ``resolve_dlt_sources`` sends each incident through the prose
  document → cognify path instead of the deterministic dlt-row schema path.

Limitations
-----------
* Post-incident reviews (PIR) stored as external attachments are not parsed.
* Log entries per incident are not retrieved; alerts provide the primary signal.
"""

import json
import os
import time
from typing import Any, Dict, Generator, List, Optional
import urllib.error
import urllib.parse
import urllib.request

try:
    from cognee.shared.logging_utils import get_logger
    logger = get_logger("pagerduty_connector")
except ImportError:
    import logging
    logger = logging.getLogger("pagerduty_connector")

try:
    from cognee.tasks.ingestion.dlt_utils import DOCUMENT_SOURCE_ATTR
except ImportError:
    DOCUMENT_SOURCE_ATTR = "cognee_document_source"

PAGERDUTY_TABLE_NAME = "pagerduty_incidents"
PAGERDUTY_SOURCE_NAME = "pagerduty"
PAGERDUTY_API_BASE = "https://api.pagerduty.com"
PAGERDUTY_PAGE_SIZE = 100

_MAX_RETRIES = 5
_EXTRA_HINT = (
    "The PagerDuty connector requires dlt: "
    "pip install 'dlt[sqlalchemy]>=1.9.0,<2'"
)

# Human-readable status labels
_STATUS_LABELS: Dict[str, str] = {
    "triggered": "Triggered (active)",
    "acknowledged": "Acknowledged (investigating)",
    "resolved": "Resolved",
}

# Urgency labels
_URGENCY_LABELS: Dict[str, str] = {
    "high": "High",
    "low": "Low",
}


def pagerduty_source(
    api_token: Optional[str] = None,
    statuses: Optional[List[str]] = None,
    urgencies: Optional[List[str]] = None,
    ingest_alerts: bool = True,
    client: Any = None,
):
    """Create a dlt source yielding PagerDuty incidents as markdown documents.

    Args:
        api_token: PagerDuty REST API token.
            Falls back to the ``PAGERDUTY_API_TOKEN`` environment variable.
        statuses: Optional list of statuses to filter, e.g.
            ``["triggered", "acknowledged", "resolved"]``.
        urgencies: Optional list of urgencies, e.g. ``["high"]``.
        ingest_alerts: When ``True``, per-incident alerts are fetched and
            included in the document.
        client: Pre-built :class:`PagerDutyClient` (test-injection point).
            When ``None`` one is built from *api_token*.

    Returns:
        A dlt source with ``cognee_document_source = "pagerduty"`` suitable
        for ``cognee.add(...)``.

    Raises:
        ValueError: If neither *api_token* nor ``PAGERDUTY_API_TOKEN`` is set
            and *client* is ``None``.
        ImportError: If ``dlt`` is not installed.
    """
    try:
        import dlt
    except ImportError as exc:
        raise ImportError(_EXTRA_HINT) from exc

    resolved_token = api_token or os.environ.get("PAGERDUTY_API_TOKEN")
    if client is None and not resolved_token:
        raise ValueError(
            "PagerDuty API token required: pass api_token= or set PAGERDUTY_API_TOKEN."
        )

    requester: PagerDutyClient = client or PagerDutyClient(api_token=resolved_token)

    @dlt.resource(name=PAGERDUTY_TABLE_NAME, primary_key="id", write_disposition="replace")
    def pagerduty_incidents() -> Generator[Dict[str, Any], None, None]:
        count = 0
        for incident in requester.iter_incidents(statuses=statuses, urgencies=urgencies):
            count += 1
            yield _incident_to_row(
                requester, incident, ingest_alerts=ingest_alerts
            )
        logger.info("PagerDuty: synced %d incident(s).", count)

    @dlt.source(name=PAGERDUTY_SOURCE_NAME)
    def _pagerduty_source():
        return pagerduty_incidents

    source = _pagerduty_source()
    setattr(source, DOCUMENT_SOURCE_ATTR, PAGERDUTY_SOURCE_NAME)
    return source


# ---------------------------------------------------------------------------
# HTTP Client
# ---------------------------------------------------------------------------


class PagerDutyClient:
    """REST API Client for PagerDuty v2 with offset pagination and retry backoff.

    Uses only Python standard library (``urllib``).
    """

    def __init__(self, api_token: str, base_url: str = PAGERDUTY_API_BASE) -> None:
        self.api_token = api_token
        self.base_url = base_url.rstrip("/")

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def iter_incidents(
        self,
        statuses: Optional[List[str]] = None,
        urgencies: Optional[List[str]] = None,
    ) -> Generator[Dict[str, Any], None, None]:
        """Yield every incident matching filters, handling offset pagination."""
        offset = 0
        while True:
            params: Dict[str, Any] = {
                "limit": PAGERDUTY_PAGE_SIZE,
                "offset": offset,
                "total": "true",
            }
            if statuses:
                params["statuses[]"] = statuses
            if urgencies:
                params["urgencies[]"] = urgencies

            data = self._request("/incidents", params=params)
            incidents: List[Dict[str, Any]] = data.get("incidents", [])
            if not incidents:
                break

            yield from incidents

            more = data.get("more", False)
            if not more:
                break
            offset += len(incidents)

    def get_incident_alerts(self, incident_id: str) -> List[Dict[str, Any]]:
        """Return alerts triggered by or associated with *incident_id*."""
        try:
            data = self._request(f"/incidents/{incident_id}/alerts")
            return data.get("alerts", [])
        except urllib.error.HTTPError as exc:
            if exc.code in (403, 404):
                logger.warning(
                    "PagerDuty: alerts for incident %s not accessible (%d).",
                    incident_id, exc.code,
                )
                return []
            raise

    # ------------------------------------------------------------------
    # Internal HTTP helpers
    # ------------------------------------------------------------------

    def _request(self, endpoint: str, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        url = self._build_url(endpoint, params)
        req = urllib.request.Request(
            url,
            headers={
                "Authorization": f"Token token={self.api_token}",
                "Accept": "application/vnd.pagerduty+json;version=2",
                "User-Agent": "cognee-community-connector-pagerduty/0.1.0",
            },
            method="GET",
        )
        for attempt in range(_MAX_RETRIES):
            try:
                with urllib.request.urlopen(req) as resp:
                    return json.loads(resp.read().decode("utf-8"))
            except urllib.error.HTTPError as exc:
                if attempt == _MAX_RETRIES - 1 or exc.code not in (429, 500, 502, 503, 504):
                    raise
                delay = self._retry_delay(exc.headers, attempt)
                logger.warning(
                    "PagerDuty: HTTP %d — retrying in %.1fs (%d/%d).",
                    exc.code, delay, attempt + 1, _MAX_RETRIES,
                )
                time.sleep(delay)
            except urllib.error.URLError:
                if attempt == _MAX_RETRIES - 1:
                    raise
                time.sleep(float(2 ** attempt))
        return {}

    def _build_url(self, endpoint: str, params: Optional[Dict[str, Any]]) -> str:
        base = f"{self.base_url}{endpoint}"
        if not params:
            return base
        parts: List[tuple] = []
        for k, v in params.items():
            if isinstance(v, list):
                parts.extend((k, item) for item in v)
            else:
                parts.append((k, str(v)))
        return f"{base}?{urllib.parse.urlencode(parts)}"

    @staticmethod
    def _retry_delay(headers: Any, attempt: int) -> float:
        header = (headers or {}).get("Retry-After") or (headers or {}).get("retry-after")
        try:
            return float(header)
        except (TypeError, ValueError):
            return float(2 ** attempt)


# ---------------------------------------------------------------------------
# Document synthesis
# ---------------------------------------------------------------------------


def _incident_to_row(
    client: Any,
    incident: Dict[str, Any],
    *,
    ingest_alerts: bool = True,
) -> Dict[str, Any]:
    """Flatten a PagerDuty incident into a Cognee document row."""
    incident_id: str = incident.get("id", "")
    title: str = incident.get("title") or incident.get("summary") or f"Incident {incident_id}"
    html_url: str = incident.get("html_url") or f"https://app.pagerduty.com/incidents/{incident_id}"

    alerts: List[Dict[str, Any]] = (
        client.get_incident_alerts(incident_id)
        if ingest_alerts and hasattr(client, "get_incident_alerts")
        else []
    )

    content = _render_markdown(incident, alerts)

    return {
        "id": incident_id,
        "url": html_url,
        "title": f"PagerDuty Incident: {title}",
        "content": content,
    }


def _render_markdown(
    incident: Dict[str, Any],
    alerts: List[Dict[str, Any]],
) -> str:
    """Render a PagerDuty incident record to a searchable Markdown document."""
    title = incident.get("title") or incident.get("summary") or "Untitled Incident"
    status = incident.get("status", "unknown")
    status_label = _STATUS_LABELS.get(status.lower(), status)
    urgency = incident.get("urgency", "unknown")
    urgency_label = _URGENCY_LABELS.get(urgency.lower(), urgency)

    created_at = incident.get("created_at") or "unknown"
    resolved_at = incident.get("last_status_change_at") if status == "resolved" else "Ongoing"
    html_url = incident.get("html_url") or ""

    # Service name
    service = incident.get("service") or {}
    service_name = service.get("summary") or service.get("name") or "Unspecified"

    # Responders / assignees
    assignments = incident.get("assignments") or []
    responders = []
    for assign in assignments:
        assignee = assign.get("assignee") or {}
        name = assignee.get("summary") or assignee.get("name") or ""
        if name:
            responders.append(name)
    responder_str = ", ".join(responders) if responders else "Unassigned"

    # Description
    description = (incident.get("description") or "").strip()

    lines = [
        f"# PagerDuty Incident: {title}",
        "",
        f"- **Status:** {status_label}",
        f"- **Urgency:** {urgency_label}",
        f"- **Service:** {service_name}",
        f"- **Lead Responders:** {responder_str}",
        f"- **Created At:** {created_at}",
        f"- **Resolved At:** {resolved_at}",
    ]
    if html_url:
        lines.append(f"- **Link:** {html_url}")

    if description:
        lines += [
            "",
            "## Description",
            description,
        ]

    if alerts:
        lines += [
            "",
            "## Triggered Alerts",
        ]
        for alert in alerts:
            alert_summary = alert.get("summary") or alert.get("details", {}).get("subject") or "Alert"
            alert_status = alert.get("status", "unknown")
            alert_created = alert.get("created_at", "")
            time_part = f" [{alert_created}]" if alert_created else ""
            lines.append(f"- **{alert_status.title()}**{time_part}: {alert_summary}")

    return "\n".join(lines)
