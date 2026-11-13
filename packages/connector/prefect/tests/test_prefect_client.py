"""Tests for the minimal synchronous Prefect REST client."""

from unittest.mock import MagicMock, patch

import pytest
import requests

from cognee_community_connector_prefect.prefect import (
    PrefectAccessError,
    PrefectAPIError,
    PrefectAuthError,
    PrefectClient,
    PrefectNotFoundError,
    PrefectRateLimitedError,
)


def test_client_init_defaults():
    client = PrefectClient()
    assert client.base_url == "http://localhost:4200/api"
    assert "redacted" not in repr(client)
    assert "<none>" in repr(client)


def test_client_init_with_key():
    client = PrefectClient(api_key="pnu_secret123")
    assert "<redacted>" in repr(client)
    assert "pnu_secret123" not in repr(client)


@pytest.mark.parametrize(
    "bad_url",
    ["", "   ", "not-a-url", "ftp://", "://broken"],
)
def test_client_rejects_bad_base_url(bad_url):
    with pytest.raises(ValueError):
        PrefectClient(base_url=bad_url)


@pytest.mark.parametrize(
    "bad_key",
    ["has space in it", "key\nwith\nnewlines", "emoji🔑"],
)
def test_client_rejects_malformed_api_key(bad_key):
    with pytest.raises(ValueError):
        PrefectClient(api_key=bad_key)


def test_client_sends_bearer_token_and_headers():
    client = PrefectClient(api_key="pnu_validkey")
    with patch("requests.request") as mock_req:
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = [{"id": "1", "name": "f1"}]
        mock_resp.headers = {"x-ratelimit-remaining": "99", "x-ratelimit-reset": "10"}
        mock_req.return_value = mock_resp

        data = client.request("POST", "/flows/filter", body={"limit": 10})
        assert data == [{"id": "1", "name": "f1"}]
        assert client.rate_limit["remaining"] == 99
        assert client.rate_limit["reset"] == 10

        mock_req.assert_called_once()
        _, kwargs = mock_req.call_args
        assert kwargs["headers"]["Authorization"] == "Bearer pnu_validkey"
        assert kwargs["headers"]["Content-Type"] == "application/json"


@pytest.mark.parametrize(
    ("status", "exc_cls"),
    [
        (401, PrefectAuthError),
        (403, PrefectAccessError),
        (404, PrefectNotFoundError),
        (429, PrefectRateLimitedError),
    ],
)
def test_client_maps_http_error_statuses(status, exc_cls):
    client = PrefectClient()
    with patch("requests.request") as mock_req:
        mock_resp = MagicMock()
        mock_resp.status_code = status
        mock_resp.json.return_value = {"detail": "error message"}
        mock_resp.headers = {}
        mock_req.return_value = mock_resp

        with pytest.raises(exc_cls):
            client.request("GET", "/flows/123")


def test_client_retries_on_500_and_succeeds():
    sleeps = []
    client = PrefectClient(sleep=lambda s: sleeps.append(s))
    with patch("requests.request") as mock_req:
        fail_resp = MagicMock()
        fail_resp.status_code = 502

        success_resp = MagicMock()
        success_resp.status_code = 200
        success_resp.json.return_value = []
        success_resp.headers = {}

        mock_req.side_effect = [fail_resp, success_resp]
        res = client.request("POST", "/flows/filter")
        assert res == []
        assert sleeps == [1]
        assert mock_req.call_count == 2


def test_client_retries_on_network_exception_until_exhausted():
    sleeps = []
    client = PrefectClient(sleep=lambda s: sleeps.append(s))
    with patch("requests.request") as mock_req:
        mock_req.side_effect = requests.ConnectionError("Connection refused")

        with pytest.raises(PrefectAPIError, match="ConnectionError"):
            client.request("POST", "/flows/filter")

        assert sleeps == [1, 2]
        assert mock_req.call_count == 3
