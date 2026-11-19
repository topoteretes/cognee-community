"""Tests for PagerDuty document formatting and DLT source."""

from unittest.mock import MagicMock

import pytest

import cognee_community_connector_pagerduty.pagerduty as pd_mod

if pd_mod.dlt is None:
    mock_dlt = MagicMock()
    mock_dlt.resource = lambda *a, **kw: lambda f: f

    def fake_source(*a, **kw):
        def dec(f):
            def wrapper(*wa, **wkw):
                s = MagicMock()
                s.name = kw.get("name", "pagerduty")
                return s

            return wrapper

        return dec

    mock_dlt.source = fake_source
    pd_mod.dlt = mock_dlt

from cognee_community_connector_pagerduty.pagerduty import (
    DOCUMENT_SOURCE_ATTR,
    PAGERDUTY_SOURCE_NAME,
    format_incident_document,
    format_service_document,
    pagerduty_source,
)


def test_format_incident_document():
    raw_incident = {
        "id": "PT4KHLG",
        "incident_number": 1024,
        "title": "High latency on payment-gateway API key=secret1234567890123456",
        "status": "triggered",
        "urgency": "high",
        "created_at": "2026-10-11T12:00:00Z",
        "last_status_change_at": "2026-10-11T12:05:00Z",
        "html_url": "https://acme.pagerduty.com/incidents/PT4KHLG",
        "service": {"id": "PSERV01", "summary": "Payment Service"},
        "escalation_policy": {"summary": "Payments Tier 1"},
        "assignments": [{"assignee": {"summary": "Alice Engineer"}}],
        "priority": {"summary": "P1"},
    }
    notes = [
        {
            "user": {"summary": "Bob SRE"},
            "content": "Investigating replica lag",
            "created_at": "2026-10-11T12:02:00Z",
        }
    ]

    doc = format_incident_document(raw_incident, notes=notes)

    assert doc["id"] == "pagerduty:incident:PT4KHLG"
    assert "Incident #1024: High latency on payment-gateway" in doc["title"]
    assert "secret1234567890123456" not in doc["content"]
    assert "[REDACTED_KEY]" in doc["content"]
    assert "- **Status**: triggered" in doc["content"]
    assert "- **Urgency**: high" in doc["content"]
    assert "- **Priority**: P1" in doc["content"]
    assert "Bob SRE" in doc["content"]
    assert "Investigating replica lag" in doc["content"]
    assert doc["external_metadata"]["source"] == "pagerduty"
    assert doc["external_metadata"]["incident_id"] == "PT4KHLG"
    assert doc["external_metadata"]["service_name"] == "Payment Service"


def test_format_service_document():
    raw_service = {
        "id": "PSERV01",
        "name": "Auth Service",
        "description": "Handles OAuth and session token verification.",
        "status": "active",
        "created_at": "2026-01-01T00:00:00Z",
        "html_url": "https://acme.pagerduty.com/services/PSERV01",
        "escalation_policy": {"summary": "Core Infrastructure Policy"},
    }

    doc = format_service_document(raw_service)

    assert doc["id"] == "pagerduty:service:PSERV01"
    assert doc["title"] == "Service: Auth Service"
    assert "Handles OAuth and session token verification." in doc["content"]
    assert doc["external_metadata"]["source"] == "pagerduty"
    assert doc["external_metadata"]["service_id"] == "PSERV01"


def test_source_requires_token_or_client():
    with pytest.raises(ValueError, match="PagerDuty API token must be provided"):
        pagerduty_source(token="")


def test_source_decorations_and_metadata():
    mock_client = MagicMock()
    mock_client.get_incidents.return_value = {"incidents": [], "more": False}
    mock_client.get_services.return_value = {"services": [], "more": False}

    source = pagerduty_source(client=mock_client)

    assert getattr(source, DOCUMENT_SOURCE_ATTR) == PAGERDUTY_SOURCE_NAME
    assert source.name == PAGERDUTY_SOURCE_NAME
