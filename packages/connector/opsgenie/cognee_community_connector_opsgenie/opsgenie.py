"""DLT source for Atlassian Opsgenie alerts, on-call schedules, and escalation policies.

Fetches Opsgenie alerts, on-call schedules, and active responder rotations, renders them
to structured markdown documents, and yields them as dlt resources for Cognee's ingestion
and knowledge graph extraction engine.

Sets ``DOCUMENT_SOURCE_ATTR = "opsgenie"`` and ``write_disposition="replace"`` so resolved,
deleted, or expired incidents are automatically cleaned up in Cognee.
"""

from typing import Any

try:
    from cognee.shared.logging_utils import get_logger

    logger = get_logger("opsgenie_connector")
except ImportError:
    import logging

    logger = logging.getLogger("opsgenie_connector")

try:
    from cognee.tasks.ingestion.dlt_utils import DOCUMENT_SOURCE_ATTR
except ImportError:
    DOCUMENT_SOURCE_ATTR = "cognee_document_source"

from cognee_community_connector_opsgenie.client import OpsgenieClient

OPSGENIE_ALERTS_TABLE_NAME = "opsgenie_alerts"
OPSGENIE_SCHEDULES_TABLE_NAME = "opsgenie_schedules"
OPSGENIE_ESCALATIONS_TABLE_NAME = "opsgenie_escalations"
OPSGENIE_SOURCE_NAME = "opsgenie"

_EXTRA_HINT = (
    "The Opsgenie connector requires dependencies: "
    'pip install "cognee-community-connector-opsgenie" (provides dlt and httpx).'
)


def opsgenie_source(
    api_key: str | None = None,
    base_url: str | None = None,
    query: str | None = None,
    include_schedules: bool = True,
    include_escalations: bool = True,
    verify_ssl: bool = True,
    service: Any = None,
    client: Any = None,
):
    """Create a dlt source yielding Opsgenie alerts and on-call schedules as documents.

    Args:
        api_key: Opsgenie API key. Falls back to ``OPSGENIE_API_KEY`` env var.
        base_url: Opsgenie API base URL (default: ``https://api.opsgenie.com``).
            Falls back to ``OPSGENIE_BASE_URL`` env var.
        query: Optional Opsgenie alert filter query (e.g. ``status: open``).
        include_schedules: Whether to ingest on-call schedules alongside alerts (default: True).
        include_escalations: Whether to ingest escalation policies alongside alerts (default: True).
        verify_ssl: Whether to verify SSL certificates (default: True).
        service: Injected client instance for testing / dependency injection.
        client: Backwards-compatible alias for ``service``.

    Returns:
        A dlt source suitable for ``cognee.add(...)``.
    """
    try:
        import dlt
    except ImportError as exc:
        raise ImportError(_EXTRA_HINT) from exc

    resolved_client = service or client
    if resolved_client is None:
        resolved_client = OpsgenieClient(
            api_key=api_key,
            base_url=base_url,
            verify_ssl=verify_ssl,
        )

    server_base = resolved_client.base_url

    @dlt.resource(
        name=OPSGENIE_ALERTS_TABLE_NAME,
        primary_key="id",
        write_disposition="replace",
    )
    def opsgenie_alerts():
        alerts = resolved_client.get_alerts(query=query)
        count = 0
        for alert in alerts:
            count += 1
            yield _alert_to_document(alert, server_base)
        logger.info("Opsgenie: synced %d alert(s).", count)

    @dlt.resource(
        name=OPSGENIE_SCHEDULES_TABLE_NAME,
        primary_key="id",
        write_disposition="replace",
    )
    def opsgenie_schedules():
        if not include_schedules:
            return
        schedules = resolved_client.get_schedules()
        count = 0
        for sched in schedules:
            sched_id = sched.get("id") or sched.get("name")
            on_calls = {}
            if sched_id:
                try:
                    on_calls = resolved_client.get_schedule_on_calls(sched_id)
                except Exception as e:
                    logger.debug("Opsgenie: could not fetch on-calls for %s: %s", sched_id, e)
            count += 1
            yield _schedule_to_document(sched, on_calls, server_base)
        logger.info("Opsgenie: synced %d schedule(s).", count)

    @dlt.resource(
        name=OPSGENIE_ESCALATIONS_TABLE_NAME,
        primary_key="id",
        write_disposition="replace",
    )
    def opsgenie_escalations():
        if not include_escalations:
            return
        escalations = resolved_client.get_escalations()
        count = 0
        for esc in escalations:
            count += 1
            yield _escalation_to_document(esc, server_base)
        logger.info("Opsgenie: synced %d escalation policy(ies).", count)

    @dlt.source(name=OPSGENIE_SOURCE_NAME)
    def _opsgenie():
        return [opsgenie_alerts, opsgenie_schedules, opsgenie_escalations]

    source = _opsgenie()
    setattr(source, DOCUMENT_SOURCE_ATTR, OPSGENIE_SOURCE_NAME)
    return source


def _alert_to_document(alert: dict[str, Any], base_url: str) -> dict[str, Any]:
    """Convert an Opsgenie alert dictionary into a structured markdown document."""
    alert_id = alert.get("id", "unknown")
    message = alert.get("message", "No message provided")
    priority = alert.get("priority", "P3")
    status = alert.get("status", "open")
    entity = alert.get("entity", "N/A")
    source = alert.get("source", "N/A")
    owner = alert.get("owner", "Unassigned")
    created_at = alert.get("createdAt", "N/A")
    updated_at = alert.get("updatedAt", "N/A")
    description = alert.get("description", "")
    tags = alert.get("tags") or []
    responders = alert.get("responders") or []

    lines = [
        f"# Opsgenie Alert: {message}",
        "",
        "## Alert Details",
        f"- **Alert ID**: {alert_id}",
        f"- **Priority**: {priority}",
        f"- **Status**: {status}",
        f"- **Entity**: {entity}",
        f"- **Source**: {source}",
        f"- **Owner**: {owner}",
        f"- **Created At**: {created_at}",
        f"- **Updated At**: {updated_at}",
    ]

    if description:
        lines.extend(
            [
                "",
                "## Description",
                description,
            ]
        )

    if responders:
        lines.extend(
            [
                "",
                "## Assigned Responders & Teams",
            ]
        )
        for resp in responders:
            r_type = resp.get("type", "responder")
            r_id = resp.get("name") or resp.get("id") or "unknown"
            lines.append(f"- **{r_type.title()}**: `{r_id}`")

    if tags:
        lines.extend(
            [
                "",
                "## Tags",
            ]
        )
        for tag in tags:
            lines.append(f"- `{tag}`")

    content = "\n".join(lines)
    web_url = f"{base_url}/alert/detail/{alert_id}/details"

    return {
        "id": f"opsgenie_alert_{alert_id}",
        "url": web_url,
        "title": f"Opsgenie Alert [{priority}]: {message}",
        "content": content,
    }


def _schedule_to_document(
    schedule: dict[str, Any],
    on_calls: dict[str, Any],
    base_url: str,
) -> dict[str, Any]:
    """Convert an Opsgenie schedule and active on-call participants to markdown."""
    sched_id = schedule.get("id", "unknown")
    name = schedule.get("name", "Unnamed Schedule")
    description = schedule.get("description", "No description provided.")
    timezone = schedule.get("timezone", "UTC")
    enabled = schedule.get("enabled", True)

    lines = [
        f"# Opsgenie Schedule: {name}",
        "",
        f"**Description**: {description}",
        f"- **Timezone**: {timezone}",
        f"- **Enabled**: {'Yes' if enabled else 'No'}",
    ]

    active_recipients = on_calls.get("onCallRecipients") or []
    if active_recipients:
        lines.extend(
            [
                "",
                "## Active On-Call Participants",
            ]
        )
        for recipient in active_recipients:
            lines.append(f"- `{recipient}`")

    rotations = schedule.get("rotations") or []
    if rotations:
        lines.extend(
            [
                "",
                "## Rotations",
            ]
        )
        for rot in rotations:
            rot_name = rot.get("name", "Rotation")
            rot_type = rot.get("type", "weekly")
            lines.append(f"- **{rot_name}** ({rot_type})")

    content = "\n".join(lines)
    web_url = f"{base_url}/schedule/detail/{sched_id}"

    return {
        "id": f"opsgenie_schedule_{sched_id}",
        "url": web_url,
        "title": f"Opsgenie Schedule: {name}",
        "content": content,
    }


def _escalation_to_document(escalation: dict[str, Any], base_url: str) -> dict[str, Any]:
    """Convert an Opsgenie escalation policy into a structured markdown document."""
    esc_id = escalation.get("id", "unknown")
    name = escalation.get("name", "Unnamed Escalation")
    description = escalation.get("description", "No description provided.")
    rules = escalation.get("rules") or []
    repeat = escalation.get("repeat") or {}

    lines = [
        f"# Opsgenie Escalation Policy: {name}",
        "",
        f"**Description**: {description}",
        "",
        "## Escalation Tiers & Rules",
    ]

    if not rules:
        lines.append("- *No escalation steps configured.*")
    else:
        for idx, rule in enumerate(rules, 1):
            condition = rule.get("condition", "if-not-acked")
            delay = rule.get("delay") or {}
            delay_amount = delay.get("timeAmount", 0)
            delay_unit = delay.get("timeUnit", "minutes")
            recipient = rule.get("recipient") or {}
            recip_type = recipient.get("type", "recipient")
            recip_id = recipient.get("name") or recipient.get("id") or "unknown"

            cond_str = "unacknowledged" if "acked" in condition else "unclosed"
            lines.append(
                f"- **Step {idx} ({delay_amount} {delay_unit} delay)**: "
                f"If alert remains {cond_str}, escalate to **{recip_type.title()}** `{recip_id}`."
            )

    if repeat:
        wait_interval = repeat.get("waitInterval", 0)
        count = repeat.get("count", 0)
        lines.extend(
            [
                "",
                "## Repeat Policy",
                f"- **Repeat Count**: {count}",
                f"- **Wait Interval**: {wait_interval} minutes",
            ]
        )

    content = "\n".join(lines)
    web_url = f"{base_url}/escalation/detail/{esc_id}"

    return {
        "id": f"opsgenie_escalation_{esc_id}",
        "url": web_url,
        "title": f"Opsgenie Escalation Policy: {name}",
        "content": content,
    }
