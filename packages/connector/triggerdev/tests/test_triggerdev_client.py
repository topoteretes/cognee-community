"""Tests for the TriggerDevClient HTTP layer."""

import pytest
import requests
from cognee_community_connector_triggerdev.triggerdev import (
    TriggerDevClient,
    TriggerDevAuthError,
    TriggerDevAccessError,
    TriggerDevRateLimitedError,
    TriggerDevNotFoundError,
    TriggerDevAPIError,
    sanitize_text,
)


class MockResponse:
    def __init__(self, status_code: int, json_data: dict | None = None, text: str = "", headers: dict | None = None):
        self.status_code = status_code
        self._json = json_data
        self.text = text or (str(json_data) if json_data else "")
        self.headers = headers or {}

    def json(self):
        if self._json is None:
            raise ValueError("No JSON")
        return self._json


def test_client_init_validation():
    with pytest.raises(ValueError, match="API key is empty"):
        TriggerDevClient("")

    with pytest.raises(ValueError, match="API key is empty"):
        TriggerDevClient("   ")

    client = TriggerDevClient("tr_prod_12345", api_url="https://api.trigger.dev/")
    assert client.api_url == "https://api.trigger.dev"
    assert "<redacted>" in repr(client)


def test_client_parse_errors(monkeypatch):
    client = TriggerDevClient("tr_prod_123")

    # 401 Unauthorized
    monkeypatch.setattr(requests, "request", lambda *args, **kwargs: MockResponse(401))
    with pytest.raises(TriggerDevAuthError, match="rejected the API key"):
        client.request("GET", "/api/v1/tasks")

    # 403 Forbidden
    monkeypatch.setattr(requests, "request", lambda *args, **kwargs: MockResponse(403))
    with pytest.raises(TriggerDevAccessError, match="access forbidden"):
        client.request("GET", "/api/v1/tasks")

    # 404 Not Found
    monkeypatch.setattr(requests, "request", lambda *args, **kwargs: MockResponse(404))
    with pytest.raises(TriggerDevNotFoundError, match="not found"):
        client.request("GET", "/api/v1/tasks/missing")

    # 429 Rate Limited with Retry-After
    monkeypatch.setattr(requests, "request", lambda *args, **kwargs: MockResponse(429, headers={"Retry-After": "30"}))
    with pytest.raises(TriggerDevRateLimitedError, match="retry after 30s"):
        client.request("GET", "/api/v1/tasks")


def test_sanitize_text():
    raw = "Failed with token tr_prod_secret123456789 and Bearer eyJhbGciOiJIUzI1Ni.api_key: 'topsecretkey123'"
    sanitized = sanitize_text(raw)
    assert "tr_prod_secret123456789" not in sanitized
    assert "eyJhbGciOiJIUzI1Ni" not in sanitized
    assert "[REDACTED]" in sanitized
