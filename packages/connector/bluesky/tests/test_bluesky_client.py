"""Unit tests for BlueskyClient REST handling, retries, and errors."""

from unittest.mock import MagicMock, patch

import pytest
import requests

from cognee_community_connector_bluesky.bluesky import (
    BlueskyAPIError,
    BlueskyClient,
    BlueskyNotFoundError,
    BlueskyRateLimitedError,
    _clean_path,
    _normalize_handle,
)


def test_client_init_valid():
    client = BlueskyClient("https://public.api.bsky.app")
    assert client.base_url == "https://public.api.bsky.app"
    assert "BlueskyClient" in repr(client)


@pytest.mark.parametrize(
    "bad_url", ["", "   ", "ftp://public.api.bsky.app", "not-a-url", "://broken"]
)
def test_client_init_bad_url(bad_url: str):
    with pytest.raises(ValueError):
        BlueskyClient(bad_url)


def test_clean_path():
    assert (
        _clean_path("/xrpc/app.bsky.feed.getAuthorFeed?actor=bsky.app")
        == "/xrpc/app.bsky.feed.getAuthorFeed"
    )


def test_normalize_handle():
    assert _normalize_handle("@bsky.app") == "bsky.app"
    assert _normalize_handle("bsky.app") == "bsky.app"
    with pytest.raises(ValueError):
        _normalize_handle("@")
    with pytest.raises(ValueError):
        _normalize_handle("has space")


def test_client_maps_404():
    client = BlueskyClient("https://public.api.bsky.app", sleep=lambda _: None)
    mock_resp = MagicMock()
    mock_resp.status_code = 404

    with patch("requests.get", return_value=mock_resp), pytest.raises(BlueskyNotFoundError):
        client.request("/xrpc/app.bsky.feed.getAuthorFeed")


def test_client_maps_429():
    client = BlueskyClient("https://public.api.bsky.app", sleep=lambda _: None)
    mock_resp = MagicMock()
    mock_resp.status_code = 429

    with patch("requests.get", return_value=mock_resp), pytest.raises(BlueskyRateLimitedError):
        client.request("/xrpc/app.bsky.feed.getAuthorFeed")


def test_client_retries_on_500_and_succeeds():
    sleep_calls = []
    client = BlueskyClient("https://public.api.bsky.app", sleep=lambda s: sleep_calls.append(s))

    err_resp = MagicMock()
    err_resp.status_code = 503

    ok_resp = MagicMock()
    ok_resp.status_code = 200
    ok_resp.json.return_value = {"feed": []}

    with patch("requests.get", side_effect=[err_resp, ok_resp]):
        res = client.request("/xrpc/app.bsky.feed.getAuthorFeed")
        assert res == {"feed": []}
        assert len(sleep_calls) == 1
        assert sleep_calls[0] == 1


def test_client_retries_on_network_exception_until_exhausted():
    sleep_calls = []
    client = BlueskyClient("https://public.api.bsky.app", sleep=lambda s: sleep_calls.append(s))

    with patch("requests.get", side_effect=requests.ConnectionError("Network down")):
        with pytest.raises(BlueskyAPIError) as exc_info:
            client.request("/xrpc/app.bsky.feed.getAuthorFeed")
        assert "ConnectionError" in str(exc_info.value)
        assert len(sleep_calls) == 2


def test_client_handles_invalid_json():
    client = BlueskyClient("https://public.api.bsky.app", sleep=lambda _: None)
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.side_effect = ValueError("Invalid JSON string")

    with patch("requests.get", return_value=mock_resp), pytest.raises(BlueskyAPIError):
        client.request("/xrpc/app.bsky.feed.getAuthorFeed")
