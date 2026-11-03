"""Hermetic unit tests for the FireHydrant incident connector for Cognee."""

import json
import urllib.error
from unittest.mock import patch, MagicMock
import pytest

from cognee_community_connector_firehydrant.firehydrant import (
    FireHydrantClient,
    firehydrant_data,
    format_incident_to_markdown,
    extract_incident_node_sets,
)


def test_client_auth_headers():
    """Verifies that headers include proper Authorization and User-Agent."""
    client_unauthed = FireHydrantClient()
    headers = client_unauthed._get_headers()
    assert headers["Accept"] == "application/json"
    assert "Authorization" not in headers

    client_authed = FireHydrantClient(api_token="fh_bot_secret_token")
    headers = client_authed._get_headers()
    assert headers["Authorization"] == "Bearer fh_bot_secret_token"


def test_fetch_incidents_bundle_enrichment():
    """Verifies that fetch_incidents_bundle enriches incidents with their timeline notes."""
    client = FireHydrantClient(api_token="mock_token")

    mock_incidents = [
        {
            "id": "inc-101",
            "name": "Database Connection Pool Exhaustion",
            "severity": "SEV1",
            "priority": "P1",
            "current_milestone": "mitigated",
            "services": [{"name": "auth-service"}, {"name": "user-db"}]
        }
    ]

    mock_notes = [
        {
            "id": "note-1",
            "body": "Scaled up database read replicas to alleviate read traffic.",
            "created_by": {"name": "Alice SRE"},
            "created_at": "2026-11-03T10:15:00Z"
        }
    ]

    with patch.object(client, "get_incidents", return_value=mock_incidents):
        with patch.object(client, "get_incident_notes", return_value=mock_notes):
            bundle = client.fetch_incidents_bundle()
            assert len(bundle) == 1
            inc = bundle[0]
            assert inc["id"] == "inc-101"
            assert len(inc["notes"]) == 1
            assert inc["notes"][0]["id"] == "note-1"


def test_format_incident_to_markdown():
    """Verifies markdown formatting of incident metadata, timeline updates, and post-mortems."""
    mock_inc = {
        "id": "inc-202",
        "name": "Redis Cache Outage",
        "summary": "Redis cluster experienced partition failure.",
        "description": "High memory consumption triggered eviction loop.",
        "severity": "SEV2",
        "priority": "P2",
        "current_milestone": "resolved",
        "created_at": "2026-11-03T08:00:00Z",
        "updated_at": "2026-11-03T09:30:00Z",
        "services": [{"name": "session-store"}],
        "environments": [{"name": "production"}],
        "notes": [
            {
                "body": "Restarted standby nodes and adjusted maxmemory policy.",
                "created_by": {"name": "Bob DevOps"},
                "created_at": "2026-11-03T08:45:00Z"
            }
        ],
        "retrospective": {
            "summary": "Action Item: Upgrade redis cluster nodes to r6g.xlarge."
        }
    }

    md = format_incident_to_markdown(mock_inc)
    assert "# FireHydrant Incident: Redis Cache Outage (`inc-202`)" in md
    assert "- **Severity**: `SEV2`" in md
    assert "- **Current Milestone**: `resolved`" in md
    assert "session-store" in md
    assert "Restarted standby nodes" in md
    assert "Upgrade redis cluster nodes" in md


def test_extract_incident_node_sets():
    """Verifies graph node set extraction for incident, severity, service, and environment."""
    mock_inc = {
        "id": "inc-303",
        "name": "Stripe Webhook Delivery Delays",
        "severity": "SEV3",
        "priority": "P2",
        "current_milestone": "investigating",
        "services": [{"name": "billing-api"}, {"name": "notification-worker"}],
        "environments": [{"name": "production"}, {"name": "us-east-1"}]
    }

    nodes = extract_incident_node_sets(mock_inc)
    assert "inc-303" in nodes["incident"]
    assert nodes["severity"] == ["SEV3"]
    assert nodes["milestone"] == ["investigating"]
    assert nodes["service"] == ["billing-api", "notification-worker"]
    assert nodes["environment"] == ["production", "us-east-1"]


def test_incremental_cursor_updated_after():
    """Verifies that updated_after filter is passed into API query parameters."""
    client = FireHydrantClient(api_token="token_test")

    with patch.object(client, "_http_get", return_value={"data": []}) as mock_get:
        client.get_incidents(updated_after="2026-11-01T00:00:00Z")
        mock_get.assert_called_once()
        args, kwargs = mock_get.call_args
        assert kwargs["params"]["updated_after"] == "2026-11-01T00:00:00Z"


def test_rate_limit_retry_handling():
    """Verifies exponential retry on HTTP 429."""
    client = FireHydrantClient(api_token="mock_token")

    err_429 = urllib.error.HTTPError(
        url="https://api.firehydrant.io/v1/incidents",
        code=429,
        msg="Too Many Requests",
        hdrs={},
        fp=None
    )

    with patch("urllib.request.urlopen") as mock_open:
        mock_resp = MagicMock()
        mock_resp.read.return_value = b'{"data": []}'
        mock_resp.__enter__.return_value = mock_resp
        mock_open.side_effect = [err_429, mock_resp]

        res = client.get_incidents()
        assert res == []
        assert mock_open.call_count == 2


def test_auth_failure_raises_permission_error():
    """Verifies that HTTP 401 raises PermissionError."""
    client = FireHydrantClient(api_token="bad_token")

    err_401 = urllib.error.HTTPError(
        url="https://api.firehydrant.io/v1/incidents",
        code=401,
        msg="Unauthorized",
        hdrs={},
        fp=None
    )

    with patch("urllib.request.urlopen", side_effect=err_401):
        with pytest.raises(PermissionError, match="FireHydrant authentication failed"):
            client.get_incidents()


def test_firehydrant_data_generator_yields_documents():
    """Verifies that firehydrant_data generator yields properly structured Cognee documents."""
    mock_bundle = [
        {
            "id": "inc-404",
            "name": "Auth Gateway 502 Errors",
            "severity": "SEV1",
            "priority": "P1",
            "current_milestone": "resolved",
            "services": [{"name": "auth-gateway"}],
            "environments": [{"name": "production"}],
            "notes": []
        }
    ]

    mock_client = MagicMock()
    mock_client.fetch_incidents_bundle.return_value = mock_bundle

    docs = list(firehydrant_data(client=mock_client))
    assert len(docs) == 1
    doc = docs[0]
    assert doc["id"] == "firehydrant_incident_inc-404"
    assert doc["type"] == "firehydrant_incident"
    assert "metadata" in doc
    assert doc["metadata"]["incident_id"] == "inc-404"
    assert "node_sets" in doc["metadata"]
