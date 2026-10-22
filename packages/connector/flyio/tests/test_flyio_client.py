"""Tests for Fly.io HTTP client and credential sanitization."""

import pytest
import requests
from unittest.mock import Mock, patch
from cognee_community_connector_flyio.flyio import (
    FlyioClient,
    FlyioAuthError,
    FlyioAccessError,
    FlyioRateLimitedError,
    FlyioNotFoundError,
    FlyioAPIError,
    sanitize_text,
)


def test_client_init_validation():
    with pytest.raises(ValueError, match="empty or has an invalid format"):
        FlyioClient("")

    client = FlyioClient("FlyV1 mock12345678")
    assert repr(client) == "FlyioClient(api_url='https://api.machines.dev/v1', key=<redacted>)"


def test_sanitize_text_redacts_tokens():
    raw = "Failed with FlyV1 test_token_secret_1234 and Bearer session_token_secret and fo1_sample_fly_key"
    sanitized = sanitize_text(raw)
    assert "test_token_secret_1234" not in sanitized
    assert "session_token_secret" not in sanitized
    assert "sample_fly_key" not in sanitized
    assert "[REDACTED]" in sanitized


@patch("requests.request")
def test_client_status_codes(mock_req):
    client = FlyioClient("FlyV1 12345678", sleep=lambda _: None)

    # 401
    mock_resp = Mock(status_code=401, headers={}, text="Unauthorized")
    mock_req.return_value = mock_resp
    with pytest.raises(FlyioAuthError):
        client.request("GET", "/v1/apps")

    # 403
    mock_resp.status_code = 403
    with pytest.raises(FlyioAccessError):
        client.request("GET", "/v1/apps")

    # 429
    mock_resp.status_code = 429
    mock_resp.headers = {"Retry-After": "20"}
    with pytest.raises(FlyioRateLimitedError, match="retry after 20s"):
        client.request("GET", "/v1/apps")

    # 404
    mock_resp.status_code = 404
    with pytest.raises(FlyioNotFoundError):
        client.request("GET", "/v1/unknown")

    # 500
    mock_resp.status_code = 500
    mock_resp.json.side_effect = ValueError()
    with pytest.raises(FlyioAPIError):
        client.request("GET", "/v1/error")
