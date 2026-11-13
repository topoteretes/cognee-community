"""Unit tests for JSRClient REST handling, retries, and errors."""

from unittest.mock import MagicMock, patch

import pytest
import requests

from cognee_community_connector_jsr.jsr import (
    JSRAPIError,
    JSRClient,
    JSRNotFoundError,
    JSRRateLimitedError,
    _clean_path,
    _normalize_package_id,
    _normalize_scope_name,
)


def test_client_init_valid():
    client = JSRClient("https://api.jsr.io")
    assert client.base_url == "https://api.jsr.io"
    assert "JSRClient" in repr(client)


@pytest.mark.parametrize("bad_url", ["", "   ", "ftp://api.jsr.io", "not-a-url", "://broken"])
def test_client_init_bad_url(bad_url: str):
    with pytest.raises(ValueError):
        JSRClient(bad_url)


def test_clean_path():
    assert _clean_path("/scopes/std/packages") == "/scopes/{param}"
    assert _clean_path("/health") == "/health"


def test_normalize_package_id():
    assert _normalize_package_id("@std/path") == ("std", "path")
    assert _normalize_package_id("std/path") == ("std", "path")
    with pytest.raises(ValueError):
        _normalize_package_id("invalid-format")
    with pytest.raises(ValueError):
        _normalize_package_id("@std/")
    with pytest.raises(ValueError):
        _normalize_package_id("/path")


def test_normalize_scope_name():
    assert _normalize_scope_name("@std") == "std"
    assert _normalize_scope_name("std") == "std"
    with pytest.raises(ValueError):
        _normalize_scope_name("@")
    with pytest.raises(ValueError):
        _normalize_scope_name("   ")


def test_client_maps_404():
    client = JSRClient("https://api.jsr.io", sleep=lambda _: None)
    mock_resp = MagicMock()
    mock_resp.status_code = 404

    with patch("requests.get", return_value=mock_resp), pytest.raises(JSRNotFoundError):
        client.request("/scopes/std/packages/nonexistent")


def test_client_maps_429():
    client = JSRClient("https://api.jsr.io", sleep=lambda _: None)
    mock_resp = MagicMock()
    mock_resp.status_code = 429

    with patch("requests.get", return_value=mock_resp), pytest.raises(JSRRateLimitedError):
        client.request("/scopes/std/packages")


def test_client_retries_on_500_and_succeeds():
    sleep_calls = []
    client = JSRClient("https://api.jsr.io", sleep=lambda s: sleep_calls.append(s))

    err_resp = MagicMock()
    err_resp.status_code = 502

    ok_resp = MagicMock()
    ok_resp.status_code = 200
    ok_resp.json.return_value = [{"name": "path"}]

    with patch("requests.get", side_effect=[err_resp, ok_resp]):
        res = client.request("/scopes/std/packages")
        assert res == [{"name": "path"}]
        assert len(sleep_calls) == 1
        assert sleep_calls[0] == 1


def test_client_retries_on_network_exception_until_exhausted():
    sleep_calls = []
    client = JSRClient("https://api.jsr.io", sleep=lambda s: sleep_calls.append(s))

    with patch("requests.get", side_effect=requests.ConnectionError("Connection dropped")):
        with pytest.raises(JSRAPIError) as exc_info:
            client.request("/scopes/std/packages")
        assert "ConnectionError" in str(exc_info.value)
        assert len(sleep_calls) == 2


def test_client_handles_invalid_json():
    client = JSRClient("https://api.jsr.io", sleep=lambda _: None)
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.side_effect = ValueError("Invalid JSON string")

    with patch("requests.get", return_value=mock_resp), pytest.raises(JSRAPIError):
        client.request("/scopes/std/packages")
