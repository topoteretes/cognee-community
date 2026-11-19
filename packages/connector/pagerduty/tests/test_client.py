"""Tests for PagerDuty REST API client."""

from unittest.mock import MagicMock

import pytest

from cognee_community_connector_pagerduty.client import PagerDutyClient


def test_client_init_requires_token():
    with pytest.raises(ValueError, match="PagerDuty API token must be provided"):
        PagerDutyClient(token="")


def test_client_headers():
    client = PagerDutyClient(token="pd_test_key_123")
    headers = client._headers()
    assert headers["Authorization"] == "Token token=pd_test_key_123"
    assert "application/vnd.pagerduty+json;version=2" in headers["Accept"]


def test_client_successful_request():
    mock_session = MagicMock()
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json.return_value = {"incidents": [{"id": "P123"}]}
    mock_session.request.return_value = mock_response

    client = PagerDutyClient(token="test_token", session=mock_session)
    res = client.get_incidents()

    assert "incidents" in res
    assert res["incidents"][0]["id"] == "P123"
    mock_session.request.assert_called_once()


def test_client_retry_on_429():
    mock_session = MagicMock()

    # First attempt: 429 with Retry-After 0.01
    resp_429 = MagicMock()
    resp_429.status_code = 429
    resp_429.headers = {"Retry-After": "0.01"}

    # Second attempt: 200 OK
    resp_200 = MagicMock()
    resp_200.status_code = 200
    resp_200.json.return_value = {"services": [{"id": "S123"}]}

    mock_session.request.side_effect = [resp_429, resp_200]

    client = PagerDutyClient(token="test_token", session=mock_session, backoff_factor=0.01)
    res = client.get_services()

    assert res["services"][0]["id"] == "S123"
    assert mock_session.request.call_count == 2


def test_client_retry_on_server_error():
    mock_session = MagicMock()

    resp_503 = MagicMock()
    resp_503.status_code = 503

    resp_200 = MagicMock()
    resp_200.status_code = 200
    resp_200.json.return_value = {"notes": [{"content": "Restarted pod"}]}

    mock_session.request.side_effect = [resp_503, resp_200]

    client = PagerDutyClient(token="test_token", session=mock_session, backoff_factor=0.01)
    notes = client.get_incident_notes("P123")

    assert len(notes) == 1
    assert notes[0]["content"] == "Restarted pod"
    assert mock_session.request.call_count == 2
