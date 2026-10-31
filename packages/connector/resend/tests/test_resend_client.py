"""Tests for ResendClient HTTP and validation logic."""

from unittest.mock import MagicMock

import pytest
import requests

from cognee_community_connector_resend.resend import (
    ResendAuthenticationError,
    ResendClient,
    ResendNotFoundError,
    ResendRateLimitError,
    ResendServerError,
)


def test_client_init_valid():
    client = ResendClient(api_key="re_valid_key_123")
    assert client.api_key == "re_valid_key_123"
    assert client.base_url == "https://api.resend.com"


@pytest.mark.parametrize("bad_key", ["", "   ", "\t", "\n", None, 123])
def test_client_rejects_empty_or_whitespace_key(bad_key):
    with pytest.raises(ValueError, match="non-empty string"):
        ResendClient(api_key=bad_key)


@pytest.mark.parametrize("key_with_space", ["re key", "re\nkey", "re\rkey"])
def test_client_rejects_newline_or_space_in_key(key_with_space):
    with pytest.raises(ValueError, match="whitespace or newlines"):
        ResendClient(api_key=key_with_space)


def test_client_rejects_non_ascii_key():
    with pytest.raises(ValueError, match="ASCII characters"):
        ResendClient(api_key="re_emoji_✉️")


@pytest.mark.parametrize("bad_url", ["ftp://api.resend.com", "invalid_url", "www.resend.com"])
def test_client_rejects_bad_base_url(bad_url):
    with pytest.raises(ValueError, match="Must start with http:// or https://"):
        ResendClient(api_key="re_valid_123", base_url=bad_url)


def test_client_request_headers():
    session = MagicMock()
    resp = MagicMock(status_code=200)
    resp.json.return_value = {"data": []}
    session.request.return_value = resp

    client = ResendClient(api_key="re_valid_123", session=session)
    client.list_domains()

    session.request.assert_called_once()
    kwargs = session.request.call_args.kwargs
    assert kwargs["headers"]["Authorization"] == "Bearer re_valid_123"
    assert kwargs["headers"]["Accept"] == "application/json"
    assert "cognee-connector-resend" in kwargs["headers"]["User-Agent"]


def test_client_auth_error_401():
    session = MagicMock()
    resp = MagicMock(status_code=401, text="Unauthorized")
    session.request.return_value = resp

    client = ResendClient(api_key="re_invalid", session=session)
    with pytest.raises(ResendAuthenticationError, match="401"):
        client.list_domains()


def test_client_auth_error_403():
    session = MagicMock()
    resp = MagicMock(status_code=403, text="Forbidden")
    session.request.return_value = resp

    client = ResendClient(api_key="re_restricted", session=session)
    with pytest.raises(ResendAuthenticationError, match="403"):
        client.list_emails()


def test_client_not_found_404():
    session = MagicMock()
    resp = MagicMock(status_code=404, text="Not Found")
    session.request.return_value = resp

    client = ResendClient(api_key="re_valid", session=session)
    with pytest.raises(ResendNotFoundError, match="404"):
        client.get_domain("missing-domain")


def test_client_rate_limit_429_exhausted():
    session = MagicMock()
    resp = MagicMock(status_code=429, text="Too Many Requests", headers={"Retry-After": "0"})
    session.request.return_value = resp

    client = ResendClient(api_key="re_valid", session=session, max_retries=1, backoff_factor=0)
    with pytest.raises(ResendRateLimitError):
        client.list_domains()
    assert session.request.call_count == 2


def test_client_rate_limit_429_success_after_retry():
    session = MagicMock()
    resp_fail = MagicMock(status_code=429, headers={"Retry-After": "0"}, text="Rate limit")
    resp_success = MagicMock(status_code=200)
    resp_success.json.return_value = {"data": [{"id": "dom-1"}]}
    session.request.side_effect = [resp_fail, resp_success]

    client = ResendClient(api_key="re_valid", session=session, max_retries=2, backoff_factor=0)
    res = client.list_domains()
    assert res["data"][0]["id"] == "dom-1"
    assert session.request.call_count == 2


def test_client_server_error_500_exhausted():
    session = MagicMock()
    resp = MagicMock(status_code=500, text="Internal Server Error")
    session.request.return_value = resp

    client = ResendClient(api_key="re_valid", session=session, max_retries=1, backoff_factor=0)
    with pytest.raises(ResendServerError, match="500"):
        client.list_domains()


def test_client_server_error_503_success_after_retry():
    session = MagicMock()
    resp_fail = MagicMock(status_code=503, text="Service Unavailable")
    resp_success = MagicMock(status_code=200)
    resp_success.json.return_value = {"data": [{"id": "aud-1"}]}
    session.request.side_effect = [resp_fail, resp_success]

    client = ResendClient(api_key="re_valid", session=session, max_retries=2, backoff_factor=0)
    res = client.list_audiences()
    assert res["data"][0]["id"] == "aud-1"


def test_client_network_exception_retry():
    session = MagicMock()
    session.request.side_effect = [
        requests.ConnectionError("Network down"),
        MagicMock(status_code=200, json=lambda: {"data": []}),
    ]

    client = ResendClient(api_key="re_valid", session=session, max_retries=2, backoff_factor=0)
    res = client.list_api_keys()
    assert res == {"data": []}


def test_client_invalid_json():
    session = MagicMock()
    resp = MagicMock(status_code=200)
    resp.json.side_effect = ValueError("Invalid JSON string")
    session.request.return_value = resp

    client = ResendClient(api_key="re_valid", session=session)
    with pytest.raises(ResendServerError, match="invalid JSON"):
        client.list_emails()


def test_client_endpoints_dispatch():
    session = MagicMock()
    resp = MagicMock(status_code=200)
    resp.json.return_value = {"data": []}
    session.request.return_value = resp

    client = ResendClient(api_key="re_valid", session=session)
    client.get_domain("dom_abc")
    assert "/domains/dom_abc" in session.request.call_args.kwargs["url"]

    client.get_email("em_abc")
    assert "/emails/em_abc" in session.request.call_args.kwargs["url"]

    client.get_audience("aud_abc")
    assert "/audiences/aud_abc" in session.request.call_args.kwargs["url"]
