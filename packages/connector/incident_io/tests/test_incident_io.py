"""Unit tests for the incident.io Cognee data-source connector.

Two layers, all runnable in CI without a live incident.io token:

* **Rendering tests** — verify that ``_render_markdown`` and
  ``_action_assignee`` produce the expected Markdown from synthetic data.
  These have zero I/O and do not require dlt.

* **Integration tests** — verify the full ``incident_io_source`` factory:
  the dlt source is constructed correctly, the ``cognee_document_source``
  attribute is set, and the snapshot pipeline yields one row per incident
  with the expected shape (mocked client).
"""

from typing import Any, Dict, List
from unittest.mock import MagicMock

import pytest

from cognee_community_connector_incident_io.incident_io import (
    INCIDENT_IO_SOURCE_NAME,
    IncidentIOClient,
    _action_assignee,
    _incident_to_row,
    _render_markdown,
    incident_io_source,
)


# ---------------------------------------------------------------------------
# Fixtures / factories
# ---------------------------------------------------------------------------


def _incident(
    *,
    incident_id: str = "inc_01",
    name: str = "Database Connection Pool Exhaustion",
    status_name: str = "resolved",
    severity_name: str = "Major",
    lead_name: str = "Alex Mercer",
    created_at: str = "2026-10-10T14:00:00Z",
    resolved_at: str = "2026-10-10T14:45:00Z",
    summary: str = "Primary Postgres pool was saturated causing 504 Gateway Timeouts.",
    permalink: str = "https://app.incident.io/incidents/inc_01",
) -> Dict[str, Any]:
    return {
        "id": incident_id,
        "name": name,
        "incident_status": {"name": status_name},
        "severity": {"name": severity_name},
        "creator": {"user": {"name": lead_name}},
        "created_at": created_at,
        "resolved_at": resolved_at,
        "summary": summary,
        "permalink": permalink,
        "incident_role_assignments": [],
    }


def _update(ts: str, message: str) -> Dict[str, Any]:
    return {"id": f"upd_{ts}", "created_at": ts, "message": message}


def _action(
    desc: str,
    status: str = "outstanding",
    assignee_name: str = "DevOps OnCall",
) -> Dict[str, Any]:
    return {
        "id": f"act_{desc[:8]}",
        "description": desc,
        "status": status,
        "assignee": {"name": assignee_name},
    }


def _mock_client(
    incidents: List[Dict[str, Any]],
    updates: List[Dict[str, Any]] | None = None,
    actions: List[Dict[str, Any]] | None = None,
) -> MagicMock:
    client = MagicMock(spec=IncidentIOClient)
    client.iter_incidents.return_value = incidents
    client.get_incident_updates.return_value = updates or []
    client.get_incident_actions.return_value = actions or []
    return client


# ---------------------------------------------------------------------------
# _render_markdown — DB-free rendering tests
# ---------------------------------------------------------------------------


def test_render_markdown_basic_fields():
    inc = _incident()
    md = _render_markdown(inc, [], [])

    assert "# Incident: Database Connection Pool Exhaustion" in md
    assert "- **Status:** Resolved" in md
    assert "- **Severity:** Major" in md
    assert "- **Lead Responder:** Alex Mercer" in md
    assert "- **Opened:** 2026-10-10T14:00:00Z" in md
    assert "- **Resolved:** 2026-10-10T14:45:00Z" in md
    assert "Primary Postgres pool was saturated" in md
    assert "https://app.incident.io/incidents/inc_01" in md


def test_render_markdown_timeline_sorted():
    updates = [
        _update("2026-10-10T14:05:00Z", "Investigating high latency."),
        _update("2026-10-10T14:40:00Z", "PgBouncer max_client_conn raised."),
    ]
    md = _render_markdown(_incident(), updates, [])

    assert "## Incident Timeline" in md
    assert "14:05:00Z" in md
    assert "14:40:00Z" in md
    # Later timestamp appears after earlier one
    assert md.index("14:05:00Z") < md.index("14:40:00Z")


def test_render_markdown_action_items():
    actions = [
        _action("Add PgBouncer saturation alert", status="completed"),
        _action("Audit long-running analytics queries", status="outstanding"),
    ]
    md = _render_markdown(_incident(), [], actions)

    assert "## Remediation Actions" in md
    assert "- [x] Add PgBouncer saturation alert" in md
    assert "- [ ] Audit long-running analytics queries" in md


def test_render_markdown_ongoing_incident():
    inc = _incident(resolved_at=None, status_name="investigating")
    md = _render_markdown(inc, [], [])
    assert "- **Resolved:** Ongoing" in md
    assert "- **Status:** Investigating" in md


def test_render_markdown_unknown_status_passthrough():
    inc = _incident(status_name="custom_internal_status")
    md = _render_markdown(inc, [], [])
    assert "custom_internal_status" in md


def test_render_markdown_empty_updates_and_actions_omits_sections():
    md = _render_markdown(_incident(), [], [])
    assert "## Incident Timeline" not in md
    assert "## Remediation Actions" not in md


# ---------------------------------------------------------------------------
# _action_assignee
# ---------------------------------------------------------------------------


def test_action_assignee_dict():
    assert _action_assignee({"assignee": {"name": "Alice"}}) == "Alice"


def test_action_assignee_dict_email_fallback():
    assert _action_assignee({"assignee": {"email": "a@b.com"}}) == "a@b.com"


def test_action_assignee_missing():
    assert _action_assignee({}) == "Unassigned"


def test_action_assignee_none():
    assert _action_assignee({"assignee": None}) == "Unassigned"


# ---------------------------------------------------------------------------
# _incident_to_row
# ---------------------------------------------------------------------------


def test_incident_to_row_shape():
    client = _mock_client([_incident()])
    row = _incident_to_row(client, _incident())

    assert row["id"] == "inc_01"
    assert row["url"] == "https://app.incident.io/incidents/inc_01"
    assert row["title"] == "Incident: Database Connection Pool Exhaustion"
    assert "Database Connection Pool Exhaustion" in row["content"]


def test_incident_to_row_calls_client():
    client = _mock_client([_incident()])
    _incident_to_row(client, _incident())

    client.get_incident_updates.assert_called_once_with("inc_01")
    client.get_incident_actions.assert_called_once_with("inc_01")


def test_incident_to_row_no_sub_resources():
    client = _mock_client([_incident()], updates=[], actions=[])
    row = _incident_to_row(client, _incident(), ingest_updates=False, ingest_actions=False)

    client.get_incident_updates.assert_not_called()
    client.get_incident_actions.assert_not_called()
    assert row["id"] == "inc_01"


# ---------------------------------------------------------------------------
# incident_io_source factory
# ---------------------------------------------------------------------------


def test_incident_io_source_requires_api_key():
    import os
    old = os.environ.pop("INCIDENT_IO_API_KEY", None)
    try:
        with pytest.raises(ValueError, match="INCIDENT_IO_API_KEY"):
            incident_io_source()
    finally:
        if old is not None:
            os.environ["INCIDENT_IO_API_KEY"] = old


def test_incident_io_source_document_source_attribute():
    client = _mock_client([_incident()])
    source = incident_io_source(api_key="dummy", client=client)
    assert getattr(source, "cognee_document_source", None) == INCIDENT_IO_SOURCE_NAME


def test_incident_io_source_yields_row_per_incident():
    incidents = [_incident(incident_id="inc_01"), _incident(incident_id="inc_02")]
    client = _mock_client(incidents)
    source = incident_io_source(api_key="dummy", client=client)

    # Collect rows from the dlt resource
    rows = list(source.resources[INCIDENT_IO_SOURCE_NAME.replace("_", "-") if "-" in "incident_io_incidents" else "incident_io_incidents"]())
    # dlt resource name comes from the @dlt.resource name parameter
    resource_name = "incident_io_incidents"
    rows = list(source.resources[resource_name]())
    assert len(rows) == 2
    assert rows[0]["id"] == "inc_01"
    assert rows[1]["id"] == "inc_02"
