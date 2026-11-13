"""DLT source for PagerDuty incidents and services.

Ingests incidents and services from PagerDuty into Cognee as structured Markdown documents.
Declares ``cognee_document_source = "pagerduty"`` so rows flow through Cognee's
cognify entity extraction and knowledge graph pipeline.
"""

import os
from collections.abc import Iterator
from typing import Any

from .client import PagerDutyClient
from .redaction import redact_secrets

PAGERDUTY_SOURCE_NAME = "pagerduty"
DOCUMENT_SOURCE_ATTR = "cognee_document_source"

try:
    import dlt
except ImportError:
    dlt = None


def format_incident_document(
    incident: dict[str, Any], notes: list[dict[str, Any]] | None = None
) -> dict[str, Any]:
    """Serialize a PagerDuty incident and its notes into a structured document."""
    inc_id = incident.get("id", "")
    inc_num = incident.get("incident_number", "")
    title = redact_secrets(incident.get("title", "Untitled Incident"))
    status = incident.get("status", "unknown")
    urgency = incident.get("urgency", "unknown")
    created_at = incident.get("created_at", "")
    last_status_change_at = incident.get("last_status_change_at", "")
    html_url = incident.get("html_url", "")

    service = incident.get("service", {})
    service_name = service.get("summary", "Unknown Service")

    escalation_policy = incident.get("escalation_policy", {})
    esc_name = escalation_policy.get("summary", "None")

    assignments = incident.get("assignments", [])
    assignees = [a.get("assignee", {}).get("summary", "") for a in assignments if a.get("assignee")]
    assignee_str = ", ".join(filter(None, assignees)) or "Unassigned"

    priority = incident.get("priority", {})
    priority_name = priority.get("summary", "None")

    lines = [
        f"# Incident #{inc_num}: {title}",
        "",
        f"- **Status**: {status}",
        f"- **Urgency**: {urgency}",
        f"- **Priority**: {priority_name}",
        f"- **Service**: {service_name}",
        f"- **Escalation Policy**: {esc_name}",
        f"- **Assignees**: {assignee_str}",
        f"- **Created At**: {created_at}",
        f"- **Last Status Change**: {last_status_change_at}",
    ]

    summary = incident.get("summary")
    if summary and summary != title:
        lines.extend(["", "### Summary", redact_secrets(summary)])

    if notes:
        lines.extend(["", "### Incident Notes"])
        for n in notes:
            author = n.get("user", {}).get("summary", "Unknown")
            note_content = redact_secrets(n.get("content", ""))
            created = n.get("created_at", "")
            lines.append(f"- **{author}** ({created}): {note_content}")

    content = "\n".join(lines)

    return {
        "id": f"pagerduty:incident:{inc_id}",
        "title": f"Incident #{inc_num}: {title}",
        "content": content,
        "url": html_url,
        "external_metadata": {
            "source": "pagerduty",
            "entity_type": "incident",
            "incident_id": inc_id,
            "incident_number": inc_num,
            "status": status,
            "urgency": urgency,
            "service_id": service.get("id"),
            "service_name": service_name,
            "last_status_change_at": last_status_change_at,
        },
    }


def format_service_document(service: dict[str, Any]) -> dict[str, Any]:
    """Serialize a PagerDuty service into a structured document."""
    svc_id = service.get("id", "")
    name = redact_secrets(service.get("name", "Untitled Service"))
    description = redact_secrets(service.get("description", "")) or "No description provided."
    status = service.get("status", "active")
    html_url = service.get("html_url", "")
    created_at = service.get("created_at", "")

    esc_policy = service.get("escalation_policy", {}).get("summary", "None")

    lines = [
        f"# Service: {name}",
        "",
        f"- **Status**: {status}",
        f"- **Escalation Policy**: {esc_policy}",
        f"- **Created At**: {created_at}",
        "",
        "### Description",
        description,
    ]

    return {
        "id": f"pagerduty:service:{svc_id}",
        "title": f"Service: {name}",
        "content": "\n".join(lines),
        "url": html_url,
        "external_metadata": {
            "source": "pagerduty",
            "entity_type": "service",
            "service_id": svc_id,
            "status": status,
        },
    }


def pagerduty_source(
    token: str | None = None,
    resources: list[str] | None = None,
    statuses: list[str] | None = None,
    urgencies: list[str] | None = None,
    since: str | None = None,
    include_notes: bool = True,
    client: PagerDutyClient | None = None,
) -> Any:
    """Create a dlt source yielding PagerDuty incidents and services as documents.

    Args:
        token: PagerDuty REST API token. Falls back to ``PAGERDUTY_API_KEY`` env var.
        resources: List of resources to ingest. Defaults to ``["incidents", "services"]``.
        statuses: Filter incident statuses, e.g. ``["triggered", "acknowledged", "resolved"]``.
        urgencies: Filter incident urgencies, e.g. ``["high", "low"]``.
        since: ISO 8601 timestamp string to filter incidents created/updated since.
        include_notes: If True, fetches notes attached to each incident.
        client: Preconfigured PagerDutyClient instance (useful for testing).

    Returns:
        A dlt source object configured with ``cognee_document_source = "pagerduty"``.
    """
    if dlt is None:
        raise ImportError("dlt is required to run pagerduty_source. Install via: pip install dlt")

    resolved_token = token or os.getenv("PAGERDUTY_API_KEY")
    if not resolved_token and client is None:
        raise ValueError(
            "PagerDuty API token must be provided via token or PAGERDUTY_API_KEY."
        )

    pd_client = client or PagerDutyClient(token=resolved_token)
    selected_resources = set(resources or ["incidents", "services"])

    @dlt.resource(name="pagerduty_incidents", write_disposition="merge", primary_key="id")
    def incidents_resource() -> Iterator[dict[str, Any]]:
        offset = 0
        limit = 100
        while True:
            response = pd_client.get_incidents(
                since=since,
                statuses=statuses,
                urgencies=urgencies,
                limit=limit,
                offset=offset,
            )
            items = response.get("incidents", [])
            for item in items:
                notes = None
                if include_notes:
                    try:
                        notes = pd_client.get_incident_notes(item.get("id", ""))
                    except Exception:
                        notes = []
                yield format_incident_document(item, notes=notes)

            more = response.get("more", False)
            if not more or not items:
                break
            offset += len(items)

    @dlt.resource(name="pagerduty_services", write_disposition="replace", primary_key="id")
    def services_resource() -> Iterator[dict[str, Any]]:
        offset = 0
        limit = 100
        while True:
            response = pd_client.get_services(limit=limit, offset=offset)
            items = response.get("services", [])
            for item in items:
                yield format_service_document(item)

            more = response.get("more", False)
            if not more or not items:
                break
            offset += len(items)

    active_resources = []
    if "incidents" in selected_resources:
        active_resources.append(incidents_resource)
    if "services" in selected_resources:
        active_resources.append(services_resource)

    @dlt.source(name=PAGERDUTY_SOURCE_NAME)
    def _pagerduty() -> list[Any]:
        return active_resources

    source = _pagerduty()
    setattr(source, DOCUMENT_SOURCE_ATTR, PAGERDUTY_SOURCE_NAME)
    return source
