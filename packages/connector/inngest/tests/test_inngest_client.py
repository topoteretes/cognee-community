"""Tests for the Inngest HTTP client and credential sanitization."""

import pytest
import requests
from unittest.mock import Mock, patch
from cognee_community_connector_inngest.inngest import (
    InngestClient,
    InngestAuthError,
    InngestAccessError,
    InngestRateLimitedError,
    InngestNotFoundError,
    InngestAPIError,
    sanitize_text,
)


def test_client_init_validation():
    with pytest.raises(ValueError, match="empty or has an invalid format"):
        InngestClient("")

    client = InngestClient("signkey-prod-test12345678")
    assert repr(client) == "InngestClient(api_url='https://api.inngest.com/v1', key=<redacted>)"


def test_sanitize_text_redacts_keys():
    raw = "Failed with signkey-prod-abcdef123456 and Bearer secret_token_xyz"
    sanitized = sanitize_text(raw)
    assert "signkey-prod-abcdef123456" not in sanitized
    assert "secret_token_xyz" not in sanitized
    assert "[REDACTED]" in sanitized


@patch("requests.request")
def test_client_status_codes(mock_req):
    client = InngestClient("signkey-prod-12345678", sleep=lambda _: None)

    # 401
    mock_resp = Mock(status_code=401, headers={}, text="Unauthorized")
    mock_req.return_value = mock_resp
    with pytest.raises(InngestAuthError):
        client.request("GET", "/v1/functions")

    # 403
    mock_resp.status_code = 403
    with pytest.raises(InngestAccessError):
        client.request("GET", "/v1/functions")

    # 429
    mock_resp.status_code = 429
    mock_resp.headers = {"Retry-After": "10"}
    with pytest.raises(InngestRateLimitedError, match="retry after 10s"):
        client.request("GET", "/v1/functions")

    # 404
    mock_resp.status_code = 404
    with pytest.raises(InngestNotFoundError):
        client.request("GET", "/v1/unknown")

    # 500
    mock_resp.status_code = 500
    mock_resp.json.side_effect = ValueError()
    with pytest.raises(InngestAPIError):
        client.request("GET", "/v1/error")
