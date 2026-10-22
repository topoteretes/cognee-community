"""Tests for DopplerClient HTTP layer."""

import pytest
import requests
from cognee_community_connector_doppler.doppler import (
    DopplerClient,
    DopplerAuthError,
    DopplerAccessError,
    DopplerRateLimitedError,
    DopplerNotFoundError,
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
    with pytest.raises(ValueError, match="API token is empty"):
        DopplerClient("")

    client = DopplerClient("dp.st.test_123")
    assert client.api_url == "https://api.doppler.com/v3"
    assert "<redacted>" in repr(client)


def test_client_parse_errors(monkeypatch):
    client = DopplerClient("dp.st.123")

    monkeypatch.setattr(requests, "request", lambda *args, **kwargs: MockResponse(401))
    with pytest.raises(DopplerAuthError, match="rejected the API token"):
        client.request("GET", "/v3/projects")

    monkeypatch.setattr(requests, "request", lambda *args, **kwargs: MockResponse(403))
    with pytest.raises(DopplerAccessError, match="access forbidden"):
        client.request("GET", "/v3/projects")

    monkeypatch.setattr(requests, "request", lambda *args, **kwargs: MockResponse(404))
    with pytest.raises(DopplerNotFoundError, match="not found"):
        client.request("GET", "/v3/projects/missing")

    monkeypatch.setattr(requests, "request", lambda *args, **kwargs: MockResponse(429, headers={"Retry-After": "10"}))
    with pytest.raises(DopplerRateLimitedError, match="retry after 10s"):
        client.request("GET", "/v3/projects")
