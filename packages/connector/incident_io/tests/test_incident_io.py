"""Unit tests for the incident.io dlt connector."""

import pytest
from unittest.mock import MagicMock
from cognee_community_connector_incident_io.incident_io import (
    INCIDENT_IO_SOURCE_NAME,
    _incident_to_row,
    _render_incident_markdown,
    incident_io_source,
)


def _sample_incident():
    return {
        "id": "inc_01HQXYZ",
        "name": "Database Connection Pool Exhaustion",
        "summary": "Primary Postgres pool was saturated causing 504 Gateway Timeouts.",
        "incident_status": {"name": "resolved"},
        "severity": {"name": "Major"},
        "created_at": "2026-10-10T14:00:00Z",
        "resolved_at": "2026-10-10T14:45:00Z",
        "creator": {"name": "Alex Mercer"},
        "permalink": "https://app.incident.io/incidents/inc_01HQXYZ",
    }


def _sample_updates():
    return [
        {
            "id": "upd_1",
            "created_at": "2026-10-10T14:05:00Z",
            "message": "Investigating high latency on api-gateway.",
        },
        {
            "id": "upd_2",
            "created_at": "2026-10-10T14:40:00Z",
            "message": "Increased max connections in PgBouncer; service stabilized.",
        },
    ]


def _sample_actions():
    return [
        {
            "id": "act_1",
            "description": "Add alert threshold for PgBouncer pool saturation",
            "status": "completed",
            "assignee": {"name": "DevOps OnCall"},
        },
        {
            "id": "act_2",
            "description": "Perform query audit on long-running analytics jobs",
            "status": "outstanding",
            "assignee": {"name": "Data Team"},
        },
    ]


def test_render_incident_markdown():
    inc = _sample_incident()
    upds = _sample_updates()
    acts = _sample_actions()

    markdown = _render_incident_markdown(inc, upds, acts)

    assert "# Incident: Database Connection Pool Exhaustion" in markdown
    assert "- **Status:** resolved" in markdown
    assert "- **Severity:** Major" in markdown
    assert "- **Lead:** Alex Mercer" in markdown
    assert "Primary Postgres pool was saturated" in markdown
    assert "Increased max connections in PgBouncer" in markdown
    assert "- [x] Add alert threshold for PgBouncer pool saturation (Assignee: DevOps OnCall)" in markdown
    assert "- [ ] Perform query audit on long-running analytics jobs (Assignee: Data Team)" in markdown


def test_incident_to_row():
    mock_client = MagicMock()
    mock_client.get_incident_updates.return_value = _sample_updates()
    mock_client.get_incident_actions.return_value = _sample_actions()

    inc = _sample_incident()
    row = _incident_to_row(mock_client, inc)

    assert row["id"] == "inc_01HQXYZ"
    assert row["url"] == "https://app.incident.io/incidents/inc_01HQXYZ"
    assert row["title"] == "Incident: Database Connection Pool Exhaustion"
    assert "Database Connection Pool Exhaustion" in row["content"]


def test_incident_io_source_structure():
    mock_client = MagicMock()
    mock_client.iter_incidents.return_value = [_sample_incident()]
    mock_client.get_incident_updates.return_value = _sample_updates()
    mock_client.get_incident_actions.return_value = _sample_actions()

    source = incident_io_source(api_key="test_key", client=mock_client)
    assert getattr(source, "cognee_document_source", None) == INCIDENT_IO_SOURCE_NAME
