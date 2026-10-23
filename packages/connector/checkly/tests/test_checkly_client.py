"""Tests for the synchronous Checkly REST API client."""

from unittest.mock import MagicMock, patch

import pytest
import requests

from cognee_community_connector_checkly.checkly import (
    ChecklyAccessError,
    ChecklyAPIError,
    ChecklyAuthError,
    ChecklyClient,
    ChecklyNotFoundError,
    ChecklyRateLimitedError,
)


def test_client_init_valid():
    client = ChecklyClient(api_key="cu_secret123")
    assert client.base_url == "https://api.checklyhq.com/v1"
    assert "<redacted>" in repr(client)
    assert "cu_secret123" not in repr(client)


def test_client_init_with_account_id():
    client = ChecklyClient(api_key="cu_secret123", account_id="acc_999")
    assert client.account_id == "acc_999"
    assert "acc_999" in repr(client)


@pytest.mark.parametrize(
    "bad_key",
    ["", "   ", "key with space", "emoji🔑", "line\nbreak"],
)
def test_client_rejects_malformed_key(bad_key):
    with pytest.raises(ValueError):
        ChecklyClient(api_key=bad_key)


@pytest.mark.parametrize(
    "bad_url",
    ["", "   ", "not-a-url", "ftp://", "://broken"],
)
def test_client_rejects_bad_base_url(bad_url):
    with pytest.raises(ValueError):
        ChecklyClient(api_key="valid_key", base_url=bad_url)


def test_client_sends_headers_and_account_id():
    client = ChecklyClient(api_key="cu_validkey", account_id="acc_abc")
    with patch("requests.request") as mock_req:
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = [{"id": "1", "name": "Check 1"}]
        mock_resp.headers = {"x-ratelimit-remaining": "50", "x-ratelimit-reset": "15"}
        mock_req.return_value = mock_resp

        data = client.request("GET", "/checks")
        assert data == [{"id": "1", "name": "Check 1"}]
        assert client.rate_limit["remaining"] == 50
        assert client.rate_limit["reset"] == 15

        mock_req.assert_called_once()
        _, kwargs = mock_req.call_args
        assert kwargs["headers"]["Authorization"] == "Bearer cu_validkey"
        assert kwargs["headers"]["X-Checkly-Account"] == "acc_abc"
        assert kwargs["headers"]["Content-Type"] == "application/json"


@pytest.mark.parametrize(
    ("status", "exc_cls"),
    [
        (401, ChecklyAuthError),
        (403, ChecklyAccessError),
        (404, ChecklyNotFoundError),
        (429, ChecklyRateLimitedError),
    ],
)
def test_client_maps_http_error_statuses(status, exc_cls):
    client = ChecklyClient(api_key="cu_validkey")
    with patch("requests.request") as mock_req:
        mock_resp = MagicMock()
        mock_resp.status_code = status
        mock_resp.json.return_value = {"message": "error detail"}
        mock_resp.headers = {}
        mock_req.return_value = mock_resp

        with pytest.raises(exc_cls):
            client.request("GET", "/checks/123")


def test_client_retries_on_502_and_succeeds():
    sleeps = []
    client = ChecklyClient(api_key="cu_validkey", sleep=lambda s: sleeps.append(s))
    with patch("requests.request") as mock_req:
        fail_resp = MagicMock()
        fail_resp.status_code = 502

        ok_resp = MagicMock()
        ok_resp.status_code = 200
        ok_resp.json.return_value = []
        ok_resp.headers = {}

        mock_req.side_effect = [fail_resp, ok_resp]
        res = client.request("GET", "/checks")
        assert res == []
        assert sleeps == [1]
        assert mock_req.call_count == 2


def test_client_retries_on_connection_error_until_exhausted():
    sleeps = []
    client = ChecklyClient(api_key="cu_validkey", sleep=lambda s: sleeps.append(s))
    with patch("requests.request") as mock_req:
        mock_req.side_effect = requests.ConnectionError("Connection aborted")

        with pytest.raises(ChecklyAPIError, match="ConnectionError"):
            client.request("GET", "/checks")

        assert sleeps == [1, 2]
        assert mock_req.call_count == 3
