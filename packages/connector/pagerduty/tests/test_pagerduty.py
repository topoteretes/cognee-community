"""Unit tests for the PagerDuty Cognee data-source connector.

Two layers, all runnable offline in CI without a live PagerDuty token:

* **Rendering tests** — verify that ``_render_markdown`` produces the expected
  Markdown document from synthetic incident records. Zero I/O, no dlt required.

* **Integration tests** — verify the ``pagerduty_source`` factory:
  the dlt source is constructed correctly, ``cognee_document_source`` is set,
  and the pipeline yields one row per incident with the expected shape.
"""

from typing import Any, Dict, List
from unittest.mock import MagicMock
import os
import pytest

from cognee_community_connector_pagerduty.pagerduty import (
    PAGERDUTY_SOURCE_NAME,
    PagerDutyClient,
    _incident_to_row,
    _render_markdown,
    pagerduty_source,
)


# ---------------------------------------------------------------------------
# Fixtures / factories
# ---------------------------------------------------------------------------


def _incident(
    *,
    incident_id: str = "P12345",
    title: str = "Redis Cache Cluster Latency Spike",
    status: str = "resolved",
    urgency: str = "high",
    service_name: str = "Caching Layer",
    responder_names: List[str] | None = None,
    created_at: str = "2026-10-18T10:00:00Z",
    last_status_change_at: str = "2026-10-18T10:45:00Z",
    description: str = "Redis memory reached 98% capacity causing evictions and timeouts.",
    html_url: str = "https://app.pagerduty.com/incidents/P12345",
) -> Dict[str, Any]:
    responders = responder_names if responder_names is not None else ["Sarah Connor", "John Doe"]
    assignments = [{"assignee": {"summary": name}} for name in responders]
    return {
        "id": incident_id,
        "type": "incident",
        "title": title,
        "status": status,
        "urgency": urgency,
        "service": {"summary": service_name},
        "assignments": assignments,
        "created_at": created_at,
        "last_status_change_at": last_status_change_at,
        "description": description,
        "html_url": html_url,
    }


def _alert(
    *,
    alert_id: str = "A9876",
    summary: str = "CPU utilization exceeded 95% on redis-cluster-node-3",
    status: str = "resolved",
    created_at: str = "2026-10-18T09:59:45Z",
) -> Dict[str, Any]:
    return {
        "id": alert_id,
        "summary": summary,
        "status": status,
        "created_at": created_at,
    }


def _mock_client(
    incidents: List[Dict[str, Any]],
    alerts: List[Dict[str, Any]] | None = None,
) -> MagicMock:
    client = MagicMock(spec=PagerDutyClient)
    client.iter_incidents.return_value = incidents
    client.get_incident_alerts.return_value = alerts or []
    return client


# ---------------------------------------------------------------------------
# _render_markdown tests
# ---------------------------------------------------------------------------


def test_render_markdown_basic_fields():
    inc = _incident()
    md = _render_markdown(inc, [])

    assert "# PagerDuty Incident: Redis Cache Cluster Latency Spike" in md
    assert "- **Status:** Resolved" in md
    assert "- **Urgency:** High" in md
    assert "- **Service:** Caching Layer" in md
    assert "- **Lead Responders:** Sarah Connor, John Doe" in md
    assert "- **Created At:** 2026-10-18T10:00:00Z" in md
    assert "- **Resolved At:** 2026-10-18T10:45:00Z" in md
    assert "https://app.pagerduty.com/incidents/P12345" in md
    assert "## Description" in md
    assert "Redis memory reached 98% capacity" in md


def test_render_markdown_status_triggered():
    inc = _incident(status="triggered")
    md = _render_markdown(inc, [])
    assert "- **Status:** Triggered (active)" in md
    assert "- **Resolved At:** Ongoing" in md


def test_render_markdown_status_acknowledged():
    inc = _incident(status="acknowledged")
    md = _render_markdown(inc, [])
    assert "- **Status:** Acknowledged (investigating)" in md
    assert "- **Resolved At:** Ongoing" in md


def test_render_markdown_status_unknown_passthrough():
    inc = _incident(status="custom_status_xyz")
    md = _render_markdown(inc, [])
    assert "custom_status_xyz" in md


def test_render_markdown_urgency_low():
    inc = _incident(urgency="low")
    md = _render_markdown(inc, [])
    assert "- **Urgency:** Low" in md


def test_render_markdown_urgency_unknown():
    inc = _incident(urgency="medium")
    md = _render_markdown(inc, [])
    assert "- **Urgency:** medium" in md


def test_render_markdown_no_responders_defaults_unassigned():
    inc = _incident(responder_names=[])
    md = _render_markdown(inc, [])
    assert "- **Lead Responders:** Unassigned" in md


def test_render_markdown_missing_service():
    inc = _incident()
    inc["service"] = None
    md = _render_markdown(inc, [])
    assert "- **Service:** Unspecified" in md


def test_render_markdown_no_description():
    inc = _incident(description="")
    md = _render_markdown(inc, [])
    assert "## Description" not in md


def test_render_markdown_with_alerts():
    alerts = [
        _alert(summary="Alert 1 triggered", status="triggered", created_at="2026-10-18T10:01:00Z"),
        _alert(summary="Alert 2 resolved", status="resolved", created_at="2026-10-18T10:20:00Z"),
    ]
    md = _render_markdown(_incident(), alerts)
    assert "## Triggered Alerts" in md
    assert "- **Triggered** [2026-10-18T10:01:00Z]: Alert 1 triggered" in md
    assert "- **Resolved** [2026-10-18T10:20:00Z]: Alert 2 resolved" in md


def test_render_markdown_empty_alerts_omits_section():
    md = _render_markdown(_incident(), [])
    assert "## Triggered Alerts" not in md


def test_render_markdown_no_link():
    inc = _incident(html_url="")
    md = _render_markdown(inc, [])
    assert "- **Link:**" not in md


# ---------------------------------------------------------------------------
# _incident_to_row tests
# ---------------------------------------------------------------------------


def test_incident_to_row_shape():
    client = _mock_client([_incident()])
    row = _incident_to_row(client, _incident())

    assert row["id"] == "P12345"
    assert row["url"] == "https://app.pagerduty.com/incidents/P12345"
    assert row["title"] == "PagerDuty Incident: Redis Cache Cluster Latency Spike"
    assert "Redis Cache Cluster Latency Spike" in row["content"]


def test_incident_to_row_calls_alerts():
    client = _mock_client([_incident()])
    _incident_to_row(client, _incident())
    client.get_incident_alerts.assert_called_once_with("P12345")


def test_incident_to_row_no_alerts_when_disabled():
    client = _mock_client([_incident()])
    row = _incident_to_row(client, _incident(), ingest_alerts=False)
    client.get_incident_alerts.assert_not_called()
    assert row["id"] == "P12345"


def test_incident_to_row_fallback_url():
    inc = _incident()
    inc["html_url"] = ""
    client = _mock_client([inc])
    row = _incident_to_row(client, inc)
    assert row["url"] == "https://app.pagerduty.com/incidents/P12345"


def test_incident_to_row_summary_fallback_title():
    inc = _incident()
    inc["title"] = ""
    inc["summary"] = "Payment Service Outage"
    client = _mock_client([inc])
    row = _incident_to_row(client, inc)
    assert row["title"] == "PagerDuty Incident: Payment Service Outage"


# ---------------------------------------------------------------------------
# PagerDutyClient helpers tests
# ---------------------------------------------------------------------------


def test_build_url_no_params():
    client = PagerDutyClient(api_token="test")
    url = client._build_url("/incidents", None)
    assert url == "https://api.pagerduty.com/incidents"


def test_build_url_with_scalar_params():
    client = PagerDutyClient(api_token="test")
    url = client._build_url("/incidents", {"limit": 50, "offset": 100})
    assert "limit=50" in url
    assert "offset=100" in url


def test_build_url_with_list_params():
    client = PagerDutyClient(api_token="test")
    url = client._build_url("/incidents", {"statuses[]": ["triggered", "acknowledged"]})
    assert "statuses%5B%5D=triggered" in url
    assert "statuses%5B%5D=acknowledged" in url


def test_retry_delay_with_header():
    assert PagerDutyClient._retry_delay({"Retry-After": "3.5"}, 1) == 3.5
    assert PagerDutyClient._retry_delay({"retry-after": "7"}, 2) == 7.0


def test_retry_delay_fallback_exponential():
    assert PagerDutyClient._retry_delay({}, 0) == 1.0
    assert PagerDutyClient._retry_delay({}, 1) == 2.0
    assert PagerDutyClient._retry_delay({}, 3) == 8.0


# ---------------------------------------------------------------------------
# pagerduty_source factory tests
# ---------------------------------------------------------------------------


def test_pagerduty_source_requires_token():
    old = os.environ.pop("PAGERDUTY_API_TOKEN", None)
    try:
        with pytest.raises(ValueError, match="PAGERDUTY_API_TOKEN"):
            pagerduty_source()
    finally:
        if old is not None:
            os.environ["PAGERDUTY_API_TOKEN"] = old


def test_pagerduty_source_env_token_used():
    old = os.environ.get("PAGERDUTY_API_TOKEN")
    os.environ["PAGERDUTY_API_TOKEN"] = "pd_token_env"
    try:
        client = _mock_client([_incident()])
        source = pagerduty_source(client=client)
        assert getattr(source, "cognee_document_source", None) == PAGERDUTY_SOURCE_NAME
    finally:
        if old is not None:
            os.environ["PAGERDUTY_API_TOKEN"] = old
        else:
            os.environ.pop("PAGERDUTY_API_TOKEN", None)


def test_pagerduty_source_document_source_attribute():
    client = _mock_client([_incident()])
    source = pagerduty_source(api_token="pd_token", client=client)
    assert getattr(source, "cognee_document_source", None) == PAGERDUTY_SOURCE_NAME


def test_pagerduty_source_yields_row_per_incident():
    incidents = [_incident(incident_id="P1"), _incident(incident_id="P2")]
    client = _mock_client(incidents)
    source = pagerduty_source(api_token="pd_token", client=client)

    rows = list(source.resources["pagerduty_incidents"]())
    assert len(rows) == 2
    assert rows[0]["id"] == "P1"
    assert rows[1]["id"] == "P2"


def test_pagerduty_source_filters_passed():
    client = _mock_client([_incident()])
    source = pagerduty_source(
        api_token="pd_token",
        statuses=["triggered"],
        urgencies=["high"],
        client=client,
    )
    list(source.resources["pagerduty_incidents"]())
    client.iter_incidents.assert_called_once_with(
        statuses=["triggered"],
        urgencies=["high"],
    )
