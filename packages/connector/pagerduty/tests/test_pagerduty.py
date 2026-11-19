import pytest
from unittest.mock import patch, MagicMock
from cognee_community_connector_pagerduty import (
    PagerDutyConnector,
    fetch_pagerduty_incidents,
    fetch_pagerduty_services,
    fetch_pagerduty_oncalls,
    format_incident_markdown,
    format_service_markdown,
)
from cognee_community_connector_pagerduty.pagerduty import get_auth_headers

MOCK_INCIDENTS = [
    {
        "id": "PT12345",
        "incident_number": 402,
        "title": "Payment gateway latency elevated beyond 2500ms",
        "status": "triggered",
        "urgency": "high",
        "created_at": "2026-10-26T10:15:00Z",
        "last_status_change_at": "2026-10-26T10:16:30Z",
        "html_url": "https://antigravity.pagerduty.com/incidents/PT12345",
        "service": {
            "id": "PSERVICE1",
            "name": "Payments Core API",
            "summary": "Payments Core API"
        },
        "assignments": [
            {
                "assignee": {
                    "id": "PUSER1",
                    "name": "Sarah Connor",
                    "summary": "Sarah Connor"
                }
            }
        ],
        "escalation_policy": {
            "id": "PEST1",
            "name": "Tier 1 On-Call Engine"
        }
    }
]

MOCK_SERVICES = [
    {
        "id": "PSERVICE1",
        "name": "Payments Core API",
        "description": "Critical Stripe/PayPal webhook and charge processor",
        "status": "active",
        "html_url": "https://antigravity.pagerduty.com/services/PSERVICE1"
    }
]

MOCK_ONCALLS = [
    {
        "user": {
            "id": "PUSER1",
            "summary": "Sarah Connor"
        },
        "escalation_level": 1
    }
]

def test_get_auth_headers():
    h1 = get_auth_headers("my_secret_token")
    assert h1["Authorization"] == "Token token=my_secret_token"
    assert h1["Accept"] == "application/vnd.pagerduty+json;version=2"

    h2 = get_auth_headers("Token token=already_formatted")
    assert h2["Authorization"] == "Token token=already_formatted"

def test_format_incident_markdown():
    inc = MOCK_INCIDENTS[0]
    md = format_incident_markdown(inc)

    assert "# PagerDuty Incident #402: Payment gateway latency elevated beyond 2500ms" in md
    assert "**Incident ID:** `PT12345`" in md
    assert "**Status:** TRIGGERED" in md
    assert "**Urgency:** HIGH" in md
    assert "**Service:** Payments Core API (`PSERVICE1`)" in md
    assert "**Assigned Responders:** Sarah Connor" in md
    assert "https://antigravity.pagerduty.com/incidents/PT12345" in md

def test_format_service_markdown():
    svc = MOCK_SERVICES[0]
    md = format_service_markdown(svc)

    assert "# PagerDuty Service: Payments Core API" in md
    assert "**Service ID:** `PSERVICE1`" in md
    assert "**Operational Status:** ACTIVE" in md
    assert "Critical Stripe/PayPal webhook and charge processor" in md

@patch("requests.Session.get")
def test_fetch_pagerduty_incidents(mock_get):
    mock_resp = MagicMock()
    mock_resp.json.return_value = {"incidents": MOCK_INCIDENTS}
    mock_resp.raise_for_status = MagicMock()
    mock_get.return_value = mock_resp

    incidents = fetch_pagerduty_incidents("fake_token")
    assert len(incidents) == 1
    assert incidents[0]["id"] == "PT12345"
    assert incidents[0]["incident_number"] == 402

@patch("requests.Session.get")
def test_fetch_pagerduty_services(mock_get):
    mock_resp = MagicMock()
    mock_resp.json.return_value = {"services": MOCK_SERVICES}
    mock_resp.raise_for_status = MagicMock()
    mock_get.return_value = mock_resp

    services = fetch_pagerduty_services("fake_token")
    assert len(services) == 1
    assert services[0]["id"] == "PSERVICE1"
    assert services[0]["name"] == "Payments Core API"

@patch("requests.Session.get")
def test_fetch_pagerduty_oncalls(mock_get):
    mock_resp = MagicMock()
    mock_resp.json.return_value = {"oncalls": MOCK_ONCALLS}
    mock_resp.raise_for_status = MagicMock()
    mock_get.return_value = mock_resp

    oncalls = fetch_pagerduty_oncalls("fake_token")
    assert len(oncalls) == 1
    assert oncalls[0]["user"]["summary"] == "Sarah Connor"

@patch("requests.Session.get")
def test_pagerduty_connector_fetch_documents(mock_get):
    resp_inc = MagicMock()
    resp_inc.json.return_value = {"incidents": MOCK_INCIDENTS}
    resp_inc.raise_for_status = MagicMock()

    resp_svc = MagicMock()
    resp_svc.json.return_value = {"services": MOCK_SERVICES}
    resp_svc.raise_for_status = MagicMock()

    mock_get.side_effect = [resp_inc, resp_svc]

    connector = PagerDutyConnector(api_token="valid_token", include_services=True)
    docs = connector.fetch_documents()

    assert len(docs) == 2
    # Verify incident doc
    inc_doc = docs[0]
    assert inc_doc["metadata"]["source"] == "pagerduty"
    assert inc_doc["metadata"]["type"] == "incident"
    assert inc_doc["metadata"]["incident_number"] == 402
    assert inc_doc["metadata"]["urgency"] == "high"
    assert len(inc_doc["id"]) == 64

    # Verify service doc
    svc_doc = docs[1]
    assert svc_doc["metadata"]["source"] == "pagerduty"
    assert svc_doc["metadata"]["type"] == "service"
    assert svc_doc["metadata"]["service_name"] == "Payments Core API"
    assert len(svc_doc["id"]) == 64

def test_pagerduty_connector_missing_token_raises():
    with patch.dict("os.environ", {}, clear=True):
        with pytest.raises(ValueError, match="PagerDuty API token must be provided"):
            PagerDutyConnector(api_token="")
