"""Unit tests for the Better Stack Cognee data-source connector.

Two layers, all runnable in CI without a live Better Stack token:

* **Rendering tests** — verify that ``_render_markdown`` produces the correct
  Markdown output from synthetic data.  Zero I/O, no dlt required.

* **Integration tests** — verify the ``better_stack_source`` factory:
  the dlt source is constructed correctly, ``cognee_document_source`` is set,
  and the snapshot pipeline yields one row per incident with the expected
  shape (mocked client).
"""

from typing import Any, Dict, List
from unittest.mock import MagicMock

import pytest

from cognee_community_connector_better_stack.better_stack import (
    BETTER_STACK_SOURCE_NAME,
    BetterStackClient,
    _incident_to_row,
    _render_markdown,
    better_stack_source,
)


# ---------------------------------------------------------------------------
# Fixtures / factories
# ---------------------------------------------------------------------------


def _incident(
    *,
    incident_id: str = "123456",
    name: str = "API Gateway 502 Bad Gateway",
    status: str = "down",
    cause: str = "Upstream backend timed out during deployment rollback.",
    started_at: str = "2026-10-12T08:00:00Z",
    resolved_at: str = "2026-10-12T08:14:00Z",
    response_content: str = "HTTP 502 Bad Gateway from cloudflare-nginx",
    affected_url: str = "https://api.example.com",
) -> Dict[str, Any]:
    return {
        "id": incident_id,
        "type": "incident",
        "attributes": {
            "name": name,
            "url": f"https://uptime.betterstack.com/incidents/{incident_id}",
            "status": status,
            "cause": cause,
            "started_at": started_at,
            "resolved_at": resolved_at,
            "response_content": response_content,
            "affected_url": affected_url,
        },
    }


def _timeline_event(occurred_at: str, action: str, comment: str = "") -> Dict[str, Any]:
    return {
        "id": f"ev_{occurred_at}",
        "type": "incident_timeline_event",
        "attributes": {
            "occurred_at": occurred_at,
            "action": action,
            "comment": comment,
        },
    }


def _mock_client(
    incidents: List[Dict[str, Any]],
    timeline: List[Dict[str, Any]] | None = None,
) -> MagicMock:
    client = MagicMock(spec=BetterStackClient)
    client.iter_incidents.return_value = incidents
    client.get_incident_timeline.return_value = timeline or []
    return client


# ---------------------------------------------------------------------------
# _render_markdown — DB-free rendering tests
# ---------------------------------------------------------------------------


def test_render_markdown_basic_fields():
    inc = _incident()
    md = _render_markdown(inc["attributes"], [])

    assert "# Better Stack Incident: API Gateway 502 Bad Gateway" in md
    assert "- **Status:** Down (outage)" in md
    assert "- **Started At:** 2026-10-12T08:00:00Z" in md
    assert "- **Resolved At:** 2026-10-12T08:14:00Z" in md
    assert "Upstream backend timed out during deployment rollback" in md
    assert "HTTP 502 Bad Gateway from cloudflare-nginx" in md
    assert "- **Affected URL:** https://api.example.com" in md


def test_render_markdown_ongoing_incident():
    attrs = _incident(resolved_at=None, status="down")["attributes"]
    attrs["resolved_at"] = None
    md = _render_markdown(attrs, [])
    assert "- **Resolved At:** Ongoing" in md


def test_render_markdown_unknown_status_passthrough():
    attrs = _incident(status="custom_check_failed")["attributes"]
    md = _render_markdown(attrs, [])
    assert "custom_check_failed" in md


def test_render_markdown_timeline_included():
    timeline = [
        _timeline_event("2026-10-12T08:00:00Z", "triggered", "Monitor check failed 3 times"),
        _timeline_event("2026-10-12T08:02:00Z", "acknowledged", "On-call acknowledged via Slack"),
        _timeline_event("2026-10-12T08:14:00Z", "resolved", "Rollback completed, HTTP 200 restored"),
    ]
    md = _render_markdown(_incident()["attributes"], timeline)

    assert "## Chronological Timeline" in md
    assert "**[2026-10-12T08:00:00Z]** Triggered — Monitor check failed 3 times" in md
    assert "**[2026-10-12T08:02:00Z]** Acknowledged — On-call acknowledged via Slack" in md
    assert "**[2026-10-12T08:14:00Z]** Resolved — Rollback completed, HTTP 200 restored" in md
    timeline_section = md[md.index("## Chronological Timeline") :]
    assert timeline_section.index("08:00:00Z") < timeline_section.index("08:02:00Z") < timeline_section.index("08:14:00Z")


def test_render_markdown_no_timeline_omits_section():
    md = _render_markdown(_incident()["attributes"], [])
    assert "## Chronological Timeline" not in md


def test_render_markdown_no_response_content():
    attrs = dict(_incident()["attributes"])
    attrs["response_content"] = ""
    md = _render_markdown(attrs, [])
    assert "## Response Content" not in md


def test_render_markdown_no_affected_url():
    attrs = dict(_incident()["attributes"])
    attrs["affected_url"] = ""
    md = _render_markdown(attrs, [])
    assert "Affected URL" not in md


# ---------------------------------------------------------------------------
# _incident_to_row
# ---------------------------------------------------------------------------


def test_incident_to_row_shape():
    client = _mock_client([_incident()])
    row = _incident_to_row(client, _incident())

    assert row["id"] == "123456"
    assert row["url"] == "https://uptime.betterstack.com/incidents/123456"
    assert row["title"] == "Better Stack Outage: API Gateway 502 Bad Gateway"
    assert "API Gateway 502 Bad Gateway" in row["content"]


def test_incident_to_row_calls_timeline():
    client = _mock_client([_incident()])
    _incident_to_row(client, _incident())
    client.get_incident_timeline.assert_called_once_with("123456")


def test_incident_to_row_no_timeline():
    client = _mock_client([_incident()])
    row = _incident_to_row(client, _incident(), ingest_timeline=False)
    client.get_incident_timeline.assert_not_called()
    assert row["id"] == "123456"


def test_incident_to_row_missing_url_uses_fallback():
    inc = _incident()
    inc["attributes"]["url"] = None
    client = _mock_client([inc])
    row = _incident_to_row(client, inc)
    assert row["url"] == "https://uptime.betterstack.com/incidents/123456"


# ---------------------------------------------------------------------------
# better_stack_source factory
# ---------------------------------------------------------------------------


def test_better_stack_source_requires_token():
    import os
    old = os.environ.pop("BETTER_STACK_API_TOKEN", None)
    try:
        with pytest.raises(ValueError, match="BETTER_STACK_API_TOKEN"):
            better_stack_source()
    finally:
        if old is not None:
            os.environ["BETTER_STACK_API_TOKEN"] = old


def test_better_stack_source_document_source_attribute():
    client = _mock_client([_incident()])
    source = better_stack_source(api_token="dummy", client=client)
    assert getattr(source, "cognee_document_source", None) == BETTER_STACK_SOURCE_NAME


def test_better_stack_source_yields_row_per_incident():
    incidents = [_incident(incident_id="1"), _incident(incident_id="2")]
    client = _mock_client(incidents)
    source = better_stack_source(api_token="dummy", client=client)

    rows = list(source.resources["better_stack_incidents"]())
    assert len(rows) == 2
    assert rows[0]["id"] == "1"
    assert rows[1]["id"] == "2"
