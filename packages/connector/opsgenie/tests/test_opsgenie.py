"""Unit tests for Cognee Atlassian Opsgenie connector (100% offline)."""

from typing import Any
from unittest.mock import MagicMock, patch

import httpx
import pytest

try:
    from cognee.tasks.ingestion.dlt_utils import DOCUMENT_SOURCE_ATTR
except ImportError:
    DOCUMENT_SOURCE_ATTR = "cognee_document_source"

from cognee_community_connector_opsgenie import (
    OPSGENIE_ALERTS_TABLE_NAME,
    OPSGENIE_ESCALATIONS_TABLE_NAME,
    OPSGENIE_SCHEDULES_TABLE_NAME,
    OPSGENIE_SOURCE_NAME,
    OpsgenieAuthError,
    OpsgenieClient,
    OpsgenieNotFoundError,
    opsgenie_source,
)
from cognee_community_connector_opsgenie.opsgenie import (
    _alert_to_document,
    _escalation_to_document,
    _schedule_to_document,
)


@pytest.fixture
def sample_escalation() -> dict[str, Any]:
    return {
        "id": "esc-tier-1",
        "name": "Production Critical Escalation",
        "description": "3-tier escalation rule for production P1 alerts",
        "rules": [
            {
                "condition": "if-not-acked",
                "delay": {"timeAmount": 0, "timeUnit": "minutes"},
                "recipient": {"type": "schedule", "name": "Primary On-Call Schedule"},
            },
            {
                "condition": "if-not-acked",
                "delay": {"timeAmount": 5, "timeUnit": "minutes"},
                "recipient": {"type": "user", "name": "secondary-oncall@company.com"},
            },
            {
                "condition": "if-not-closed",
                "delay": {"timeAmount": 15, "timeUnit": "minutes"},
                "recipient": {"type": "team", "name": "SRE Leadership Team"},
            },
        ],
        "repeat": {
            "waitInterval": 15,
            "count": 2,
        },
    }


@pytest.fixture
def sample_alert() -> dict[str, Any]:
    return {
        "id": "alert-101-xyz",
        "message": "High CPU utilization on checkout service",
        "priority": "P1",
        "status": "open",
        "entity": "checkout-api-us-east",
        "source": "Datadog Alert Integration",
        "owner": "alice@company.com",
        "createdAt": "2024-10-11T12:00:00Z",
        "updatedAt": "2024-10-11T12:05:00Z",
        "description": "Checkout service pods CPU > 95% for 5 consecutive minutes.",
        "tags": ["production", "critical", "checkout"],
        "responders": [
            {"type": "team", "name": "Payments SRE Team"},
            {"type": "user", "name": "alice@company.com"},
        ],
    }


@pytest.fixture
def sample_schedule() -> dict[str, Any]:
    return {
        "id": "sched-202-abc",
        "name": "Payments Tier 1 On-Call",
        "description": "Primary on-call escalation rotation for payment services",
        "timezone": "America/New_York",
        "enabled": True,
        "rotations": [
            {"name": "Primary Weekly Rotation", "type": "weekly"},
        ],
    }


@pytest.fixture
def sample_on_calls() -> dict[str, Any]:
    return {
        "onCallRecipients": [
            "alice@company.com",
            "bob@company.com",
        ]
    }


# ===========================================================================
# Client validation & security tests
# ===========================================================================


def test_client_missing_api_key(monkeypatch):
    monkeypatch.delenv("OPSGENIE_API_KEY", raising=False)
    with pytest.raises(ValueError, match="Opsgenie API key required"):
        OpsgenieClient(api_key=None)


def test_client_invalid_base_url():
    with pytest.raises(ValueError, match="Invalid Opsgenie base URL"):
        OpsgenieClient(api_key="valid-key", base_url="ftp://invalid.host")

    with pytest.raises(ValueError, match="Invalid Opsgenie base URL"):
        OpsgenieClient(api_key="valid-key", base_url="not-a-url")


def test_client_initialization_success():
    client = OpsgenieClient(
        api_key="genie-key-123",
        base_url="https://api.eu.opsgenie.com/",
        verify_ssl=False,
    )
    assert client.api_key == "genie-key-123"
    assert client.base_url == "https://api.eu.opsgenie.com"
    assert client.verify_ssl is False
    assert client._headers()["Authorization"] == "GenieKey genie-key-123"


def test_client_token_redaction():
    client = OpsgenieClient(api_key="secret-genie-key-999")
    msg = "Failed with token secret-genie-key-999 in URL"
    redacted = client._redact(msg)
    assert "secret-genie-key-999" not in redacted
    assert "[REDACTED]" in redacted


# ===========================================================================
# Client HTTP operations & error handling
# ===========================================================================


def test_client_get_alerts_with_query():
    client = OpsgenieClient(api_key="key-123")
    mock_data = {
        "data": [
            {"id": "a-1", "message": "Alert 1"},
            {"id": "a-2", "message": "Alert 2"},
        ]
    }
    with patch.object(client, "request", return_value=mock_data) as mock_req:
        res = client.get_alerts(query="status: open AND priority: P1", limit=50)
        mock_req.assert_called_once_with(
            "GET",
            "/v2/alerts",
            params={
                "limit": 50,
                "sort": "createdAt",
                "order": "desc",
                "query": "status: open AND priority: P1",
            },
        )
        assert len(res) == 2


def test_client_get_alert_detail():
    client = OpsgenieClient(api_key="key-123")
    with patch.object(client, "request", return_value={"data": {"id": "a-1"}}) as mock_req:
        res = client.get_alert("a-1")
        mock_req.assert_called_once_with("GET", "/v2/alerts/a-1")
        assert res["id"] == "a-1"


def test_client_get_schedules_and_on_calls():
    client = OpsgenieClient(api_key="key-123")
    with patch.object(client, "request", return_value={"data": [{"id": "s-1"}]}) as mock_req:
        res = client.get_schedules()
        mock_req.assert_called_once_with("GET", "/v2/schedules")
        assert len(res) == 1

    with patch.object(
        client, "request", return_value={"data": {"onCallRecipients": ["alice"]}}
    ) as mock_req:
        res = client.get_schedule_on_calls("s-1")
        mock_req.assert_called_once_with("GET", "/v2/schedules/s-1/on-calls")
        assert res["onCallRecipients"] == ["alice"]


def test_client_auth_error_handling():
    client = OpsgenieClient(api_key="invalid-key")
    mock_resp = MagicMock(spec=httpx.Response)
    mock_resp.status_code = 401
    with patch("httpx.Client.request", return_value=mock_resp), pytest.raises(OpsgenieAuthError):
        client.request("GET", "/v2/alerts")


def test_client_not_found_error_handling():
    client = OpsgenieClient(api_key="valid-key")
    mock_resp = MagicMock(spec=httpx.Response)
    mock_resp.status_code = 404
    with (
        patch("httpx.Client.request", return_value=mock_resp),
        pytest.raises(OpsgenieNotFoundError),
    ):
        client.request("GET", "/v2/alerts/nonexistent")


# ===========================================================================
# Markdown document rendering
# ===========================================================================


def test_alert_to_document_rendering(sample_alert):
    doc = _alert_to_document(sample_alert, "https://api.opsgenie.com")
    assert doc["id"] == "opsgenie_alert_alert-101-xyz"
    assert doc["title"] == "Opsgenie Alert [P1]: High CPU utilization on checkout service"
    content = doc["content"]
    assert "# Opsgenie Alert: High CPU utilization on checkout service" in content
    assert "**Priority**: P1" in content
    assert "**Status**: open" in content
    assert "**Entity**: checkout-api-us-east" in content
    assert "**Owner**: alice@company.com" in content
    assert "Checkout service pods CPU > 95%" in content
    assert "**Team**: `Payments SRE Team`" in content
    assert "`production`" in content


def test_schedule_to_document_rendering(sample_schedule, sample_on_calls):
    doc = _schedule_to_document(sample_schedule, sample_on_calls, "https://api.opsgenie.com")
    assert doc["id"] == "opsgenie_schedule_sched-202-abc"
    assert doc["title"] == "Opsgenie Schedule: Payments Tier 1 On-Call"
    content = doc["content"]
    assert "# Opsgenie Schedule: Payments Tier 1 On-Call" in content
    assert "Primary on-call escalation rotation for payment services" in content
    assert "- **Timezone**: America/New_York" in content
    assert "`alice@company.com`" in content
    assert "**Primary Weekly Rotation** (weekly)" in content


def test_escalation_to_document_rendering(sample_escalation):
    doc = _escalation_to_document(sample_escalation, "https://api.opsgenie.com")
    assert doc["id"] == "opsgenie_escalation_esc-tier-1"
    assert doc["title"] == "Opsgenie Escalation Policy: Production Critical Escalation"
    content = doc["content"]
    assert "# Opsgenie Escalation Policy: Production Critical Escalation" in content
    assert "3-tier escalation rule for production P1 alerts" in content
    assert "Step 1 (0 minutes delay)" in content
    assert "Step 2 (5 minutes delay)" in content
    assert "Step 3 (15 minutes delay)" in content
    assert "**Repeat Count**: 2" in content
    assert "**Wait Interval**: 15 minutes" in content


# ===========================================================================
# dlt source and integration pipeline invariants
# ===========================================================================


def test_opsgenie_source_attributes():
    mock_client = MagicMock(spec=OpsgenieClient)
    mock_client.base_url = "https://api.opsgenie.com"
    source = opsgenie_source(client=mock_client)

    # Invariant 1: Document mode attribute tag
    assert getattr(source, DOCUMENT_SOURCE_ATTR) == OPSGENIE_SOURCE_NAME

    # Invariant 2: Resource definitions & table names
    resources = {r.name: r for r in source.selected_resources.values()}
    assert OPSGENIE_ALERTS_TABLE_NAME in resources
    assert OPSGENIE_SCHEDULES_TABLE_NAME in resources
    assert OPSGENIE_ESCALATIONS_TABLE_NAME in resources

    # Invariant 3: Write disposition is replace for orphan cleanup
    assert resources[OPSGENIE_ALERTS_TABLE_NAME].write_disposition == "replace"
    assert resources[OPSGENIE_SCHEDULES_TABLE_NAME].write_disposition == "replace"
    assert resources[OPSGENIE_ESCALATIONS_TABLE_NAME].write_disposition == "replace"


def test_opsgenie_source_execution(
    sample_alert, sample_schedule, sample_on_calls, sample_escalation
):
    mock_client = MagicMock(spec=OpsgenieClient)
    mock_client.base_url = "https://api.opsgenie.com"
    mock_client.get_alerts.return_value = [sample_alert]
    mock_client.get_schedules.return_value = [sample_schedule]
    mock_client.get_schedule_on_calls.return_value = sample_on_calls
    mock_client.get_escalations.return_value = [sample_escalation]

    source = opsgenie_source(client=mock_client)
    alerts_resource = source.selected_resources[OPSGENIE_ALERTS_TABLE_NAME]
    schedules_resource = source.selected_resources[OPSGENIE_SCHEDULES_TABLE_NAME]
    escalations_resource = source.selected_resources[OPSGENIE_ESCALATIONS_TABLE_NAME]

    alert_rows = list(alerts_resource)
    schedule_rows = list(schedules_resource)
    escalation_rows = list(escalations_resource)

    assert len(alert_rows) == 1
    assert alert_rows[0]["id"] == "opsgenie_alert_alert-101-xyz"
    assert alert_rows[0]["title"] == "Opsgenie Alert [P1]: High CPU utilization on checkout service"

    assert len(schedule_rows) == 1
    assert schedule_rows[0]["id"] == "opsgenie_schedule_sched-202-abc"
    assert schedule_rows[0]["title"] == "Opsgenie Schedule: Payments Tier 1 On-Call"

    assert len(escalation_rows) == 1
    assert escalation_rows[0]["id"] == "opsgenie_escalation_esc-tier-1"
    assert (
        escalation_rows[0]["title"] == "Opsgenie Escalation Policy: Production Critical Escalation"
    )
