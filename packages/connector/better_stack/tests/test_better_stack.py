"""Unit tests for the Better Stack dlt connector."""

import pytest
from unittest.mock import MagicMock
from cognee_community_connector_better_stack.better_stack import (
    BETTER_STACK_SOURCE_NAME,
    _incident_to_row,
    _render_better_stack_markdown,
    better_stack_source,
)


def _sample_incident():
    return {
        "id": "123456",
        "type": "incident",
        "attributes": {
            "name": "API Gateway 502 Bad Gateway",
            "url": "https://uptime.betterstack.com/incidents/123456",
            "cause": "Upstream backend timed out during deployment rollback.",
            "status": "resolved",
            "started_at": "2026-10-12T08:00:00Z",
            "resolved_at": "2026-10-12T08:14:00Z",
            "response_content": "HTTP 502 Bad Gateway from cloudflare-nginx",
        },
    }


def _sample_timeline():
    return [
        {
            "id": "tl_1",
            "type": "incident_timeline_event",
            "attributes": {
                "action": "triggered",
                "occurred_at": "2026-10-12T08:00:00Z",
                "comment": "Monitor check failed 3 consecutive times",
            },
        },
        {
            "id": "tl_2",
            "type": "incident_timeline_event",
            "attributes": {
                "action": "acknowledged",
                "occurred_at": "2026-10-12T08:02:00Z",
                "comment": "Engineer on-call acknowledged via Slack",
            },
        },
        {
            "id": "tl_3",
            "type": "incident_timeline_event",
            "attributes": {
                "action": "resolved",
                "occurred_at": "2026-10-12T08:14:00Z",
                "comment": "Rollback completed and healthy probes returned HTTP 200",
            },
        },
    ]


def test_render_better_stack_markdown():
    inc = _sample_incident()
    timeline = _sample_timeline()

    markdown = _render_better_stack_markdown(inc["attributes"], timeline)

    assert "# Better Stack Incident: API Gateway 502 Bad Gateway" in markdown
    assert "- **Status:** resolved" in markdown
    assert "Upstream backend timed out during deployment rollback" in markdown
    assert "HTTP 502 Bad Gateway from cloudflare-nginx" in markdown
    assert "- **[2026-10-12T08:00:00Z]** Triggered - Monitor check failed 3 consecutive times" in markdown
    assert "- **[2026-10-12T08:14:00Z]** Resolved - Rollback completed" in markdown


def test_incident_to_row():
    mock_client = MagicMock()
    mock_client.get_incident_timeline.return_value = _sample_timeline()

    inc = _sample_incident()
    row = _incident_to_row(mock_client, inc)

    assert row["id"] == "123456"
    assert row["url"] == "https://uptime.betterstack.com/incidents/123456"
    assert row["title"] == "Better Stack Outage: API Gateway 502 Bad Gateway"
    assert "API Gateway 502 Bad Gateway" in row["content"]


def test_better_stack_source_structure():
    mock_client = MagicMock()
    mock_client.iter_incidents.return_value = [_sample_incident()]
    mock_client.get_incident_timeline.return_value = _sample_timeline()

    source = better_stack_source(api_token="test_token", client=mock_client)
    assert getattr(source, "cognee_document_source", None) == BETTER_STACK_SOURCE_NAME
