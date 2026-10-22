"""Tests for Axiom HTTP client and credential sanitization."""

import pytest
import requests
from unittest.mock import Mock, patch
from cognee_community_connector_axiom.axiom import (
    AxiomClient,
    AxiomAuthError,
    AxiomAccessError,
    AxiomRateLimitedError,
    AxiomNotFoundError,
    AxiomAPIError,
    sanitize_text,
)


def test_client_init_validation():
    with pytest.raises(ValueError, match="empty or has an invalid format"):
        AxiomClient("")

    client = AxiomClient("xapt-test12345678")
    assert repr(client) == "AxiomClient(api_url='https://api.axiom.co/v1', key=<redacted>)"


def test_sanitize_text_redacts_tokens():
    raw = "Failed with token xapt-abcdef123456 and Bearer secret_session_token"
    sanitized = sanitize_text(raw)
    assert "xapt-abcdef123456" not in sanitized
    assert "secret_session_token" not in sanitized
    assert "[REDACTED]" in sanitized


@patch("requests.request")
def test_client_status_codes(mock_req):
    client = AxiomClient("xapt-12345678", sleep=lambda _: None)

    # 401
    mock_resp = Mock(status_code=401, headers={}, text="Unauthorized")
    mock_req.return_value = mock_resp
    with pytest.raises(AxiomAuthError):
        client.request("GET", "/v1/datasets")

    # 403
    mock_resp.status_code = 403
    with pytest.raises(AxiomAccessError):
        client.request("GET", "/v1/datasets")

    # 429
    mock_resp.status_code = 429
    mock_resp.headers = {"Retry-After": "15"}
    with pytest.raises(AxiomRateLimitedError, match="retry after 15s"):
        client.request("GET", "/v1/datasets")

    # 404
    mock_resp.status_code = 404
    with pytest.raises(AxiomNotFoundError):
        client.request("GET", "/v1/unknown")

    # 500
    mock_resp.status_code = 500
    mock_resp.json.side_effect = ValueError()
    with pytest.raises(AxiomAPIError):
        client.request("GET", "/v1/error")
