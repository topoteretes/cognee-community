"""Tests for the synchronous UptimeRobot API client."""

from unittest.mock import MagicMock, patch

import pytest
import requests

from cognee_community_connector_uptimerobot.uptimerobot import (
    UptimeRobotAccessError,
    UptimeRobotAPIError,
    UptimeRobotAuthError,
    UptimeRobotClient,
    UptimeRobotNotFoundError,
    UptimeRobotRateLimitedError,
)


def test_client_init_valid():
    client = UptimeRobotClient(api_key="u123456-secretkey")
    assert client.base_url == "https://api.uptimerobot.com/v2"
    assert "<redacted>" in repr(client)
    assert "u123456-secretkey" not in repr(client)


@pytest.mark.parametrize(
    "bad_key",
    ["", "   ", "key with space", "emoji🔑", "line\nbreak"],
)
def test_client_rejects_malformed_key(bad_key):
    with pytest.raises(ValueError):
        UptimeRobotClient(api_key=bad_key)


@pytest.mark.parametrize(
    "bad_url",
    ["", "   ", "not-a-url", "ftp://", "://broken"],
)
def test_client_rejects_bad_base_url(bad_url):
    with pytest.raises(ValueError):
        UptimeRobotClient(api_key="valid_key", base_url=bad_url)


def test_client_sends_payload_with_key():
    client = UptimeRobotClient(api_key="u_test_key")
    with patch("requests.post") as mock_post:
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"stat": "ok", "monitors": [{"id": 1, "friendly_name": "M1"}]}
        mock_resp.headers = {"x-ratelimit-remaining": "100", "x-ratelimit-reset": "20"}
        mock_post.return_value = mock_resp

        data = client.request("getMonitors", body={"limit": 10})
        assert data["stat"] == "ok"
        assert len(data["monitors"]) == 1
        assert client.rate_limit["remaining"] == 100

        mock_post.assert_called_once()
        _, kwargs = mock_post.call_args
        assert kwargs["json"]["api_key"] == "u_test_key"
        assert kwargs["json"]["format"] == "json"
        assert kwargs["json"]["limit"] == 10


@pytest.mark.parametrize(
    ("status", "exc_cls"),
    [
        (401, UptimeRobotAuthError),
        (403, UptimeRobotAccessError),
        (404, UptimeRobotNotFoundError),
        (429, UptimeRobotRateLimitedError),
    ],
)
def test_client_maps_http_error_statuses(status, exc_cls):
    client = UptimeRobotClient(api_key="u_validkey")
    with patch("requests.post") as mock_post:
        mock_resp = MagicMock()
        mock_resp.status_code = status
        mock_resp.json.return_value = {"stat": "fail"}
        mock_resp.headers = {}
        mock_post.return_value = mock_resp

        with pytest.raises(exc_cls):
            client.request("getMonitors")


def test_client_maps_stat_fail_errors():
    client = UptimeRobotClient(api_key="u_validkey")
    with patch("requests.post") as mock_post:
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.headers = {}

        # Auth error
        mock_resp.json.return_value = {"stat": "fail", "error": {"message": "Invalid api_key"}}
        mock_post.return_value = mock_resp
        with pytest.raises(UptimeRobotAuthError):
            client.request("getMonitors")

        # Rate limit error
        mock_resp.json.return_value = {
            "stat": "fail",
            "error": {"type": "rate_limit", "message": "Limit exceeded"},
        }
        with pytest.raises(UptimeRobotRateLimitedError):
            client.request("getMonitors")


def test_client_retries_on_502_and_succeeds():
    sleeps = []
    client = UptimeRobotClient(api_key="u_validkey", sleep=lambda s: sleeps.append(s))
    with patch("requests.post") as mock_post:
        fail_resp = MagicMock()
        fail_resp.status_code = 502

        ok_resp = MagicMock()
        ok_resp.status_code = 200
        ok_resp.json.return_value = {"stat": "ok", "monitors": []}
        ok_resp.headers = {}

        mock_post.side_effect = [fail_resp, ok_resp]
        res = client.request("getMonitors")
        assert res["stat"] == "ok"
        assert sleeps == [1]
        assert mock_post.call_count == 2


def test_client_retries_on_connection_error_until_exhausted():
    sleeps = []
    client = UptimeRobotClient(api_key="u_validkey", sleep=lambda s: sleeps.append(s))
    with patch("requests.post") as mock_post:
        mock_post.side_effect = requests.ConnectionError("Connection aborted")

        with pytest.raises(UptimeRobotAPIError, match="ConnectionError"):
            client.request("getMonitors")

        assert sleeps == [1, 2]
        assert mock_post.call_count == 3
