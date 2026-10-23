"""Standalone test runner for PagerDuty connector."""

import sys
import unittest
from unittest.mock import MagicMock

import cognee_community_connector_pagerduty.pagerduty as pd_mod

# Mock dlt if not installed in the testing environment
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

from cognee_community_connector_pagerduty.client import PagerDutyClient
from cognee_community_connector_pagerduty.pagerduty import (
    DOCUMENT_SOURCE_ATTR,
    PAGERDUTY_SOURCE_NAME,
    format_incident_document,
    format_service_document,
    pagerduty_source,
)
from cognee_community_connector_pagerduty.redaction import redact_secrets


class TestPagerDutyConnector(unittest.TestCase):
    def test_redact_secrets(self):
        text = "Token: Bearer 12345678901234567890 and AKIAIOSFODNN7EXAMPLE"
        redacted = redact_secrets(text)
        self.assertNotIn("12345678901234567890", redacted)
        self.assertNotIn("AKIAIOSFODNN7EXAMPLE", redacted)
        self.assertIn("Bearer [REDACTED]", redacted)
        self.assertIn("[REDACTED_AWS_KEY]", redacted)

    def test_client_headers(self):
        client = PagerDutyClient(token="test_token_123")
        headers = client._headers()
        self.assertEqual(headers["Authorization"], "Token token=test_token_123")
        self.assertIn("application/vnd.pagerduty+json;version=2", headers["Accept"])

    def test_client_retry_429(self):
        mock_session = MagicMock()
        resp_429 = MagicMock()
        resp_429.status_code = 429
        resp_429.headers = {"Retry-After": "0.01"}

        resp_200 = MagicMock()
        resp_200.status_code = 200
        resp_200.json.return_value = {"services": [{"id": "S123"}]}

        mock_session.request.side_effect = [resp_429, resp_200]
        client = PagerDutyClient(token="test_token", session=mock_session, backoff_factor=0.01)
        res = client.get_services()
        self.assertEqual(res["services"][0]["id"], "S123")
        self.assertEqual(mock_session.request.call_count, 2)

    def test_format_incident(self):
        inc = {
            "id": "INC101",
            "incident_number": 42,
            "title": "Database connection spike token=sk-12345678901234567890123456789012",
            "status": "triggered",
            "urgency": "high",
            "created_at": "2026-10-11T10:00:00Z",
            "last_status_change_at": "2026-10-11T10:05:00Z",
            "service": {"id": "S1", "summary": "DB Cluster"},
            "assignments": [{"assignee": {"summary": "DevOps Lead"}}],
        }
        notes = [
            {
                "user": {"summary": "Alice"},
                "content": "Running failover",
                "created_at": "2026-10-11T10:02:00Z",
            }
        ]
        doc = format_incident_document(inc, notes=notes)

        self.assertEqual(doc["id"], "pagerduty:incident:INC101")
        self.assertIn("Incident #42", doc["title"])
        self.assertNotIn("sk-1234567890", doc["content"])
        self.assertIn("Running failover", doc["content"])
        self.assertEqual(doc["external_metadata"]["source"], "pagerduty")

    def test_format_service(self):
        svc = {
            "id": "SVC1",
            "name": "Auth API",
            "description": "User authentication endpoints",
            "status": "active",
            "created_at": "2026-01-01T00:00:00Z",
        }
        doc = format_service_document(svc)
        self.assertEqual(doc["id"], "pagerduty:service:SVC1")
        self.assertIn("Auth API", doc["title"])
        self.assertEqual(doc["external_metadata"]["entity_type"], "service")

    def test_source_decorations(self):
        mock_client = MagicMock()
        mock_client.get_incidents.return_value = {"incidents": [], "more": False}
        mock_client.get_services.return_value = {"services": [], "more": False}

        source = pagerduty_source(client=mock_client)
        self.assertEqual(getattr(source, DOCUMENT_SOURCE_ATTR), PAGERDUTY_SOURCE_NAME)


if __name__ == "__main__":
    loader = unittest.TestLoader()
    suite = loader.loadTestsFromTestCase(TestPagerDutyConnector)
    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(suite)
    sys.exit(0 if result.wasSuccessful() else 1)
