"""Tests for KnockClient HTTP and validation logic."""

from unittest.mock import MagicMock

import pytest
import requests

from cognee_community_connector_knock.knock import (
    KnockAuthenticationError,
    KnockClient,
    KnockNotFoundError,
    KnockRateLimitError,
    KnockServerError,
)


def test_client_init_valid():
    client = KnockClient(api_key="sk_valid_key_123")
    assert client.api_key == "sk_valid_key_123"
    assert client.base_url == "https://api.knock.app/v1"


@pytest.mark.parametrize("bad_key", ["", "   ", "\t", "\n", None, 123])
def test_client_rejects_empty_or_whitespace_key(bad_key):
    with pytest.raises(ValueError, match="non-empty string"):
        KnockClient(api_key=bad_key)


@pytest.mark.parametrize("key_with_space", ["sk key", "sk\nkey", "sk\rkey"])
def test_client_rejects_newline_or_space_in_key(key_with_space):
    with pytest.raises(ValueError, match="whitespace or newlines"):
        KnockClient(api_key=key_with_space)


def test_client_rejects_non_ascii_key():
    with pytest.raises(ValueError, match="ASCII characters"):
        KnockClient(api_key="sk_emoji_🔑")


@pytest.mark.parametrize("bad_url", ["ftp://api.knock.app", "invalid_url", "www.knock.app"])
def test_client_rejects_bad_base_url(bad_url):
    with pytest.raises(ValueError, match="Must start with http:// or https://"):
        KnockClient(api_key="sk_valid_123", base_url=bad_url)


def test_client_request_headers():
    session = MagicMock()
    resp = MagicMock()
    resp.status_code = 200
    resp.json.return_value = {"entries": []}
    session.request.return_value = resp

    client = KnockClient(api_key="sk_valid_123", session=session)
    client.list_workflows()

    session.request.assert_called_once()
    kwargs = session.request.call_args.kwargs
    assert kwargs["headers"]["Authorization"] == "Bearer sk_valid_123"
    assert kwargs["headers"]["Accept"] == "application/json"
    assert "cognee-connector-knock" in kwargs["headers"]["User-Agent"]


def test_client_auth_error_401():
    session = MagicMock()
    resp = MagicMock()
    resp.status_code = 401
    resp.text = "Unauthorized"
    session.request.return_value = resp

    client = KnockClient(api_key="sk_invalid", session=session)
    with pytest.raises(KnockAuthenticationError, match="401"):
        client.list_workflows()


def test_client_auth_error_403():
    session = MagicMock()
    resp = MagicMock()
    resp.status_code = 403
    resp.text = "Forbidden"
    session.request.return_value = resp

    client = KnockClient(api_key="sk_restricted", session=session)
    with pytest.raises(KnockAuthenticationError, match="403"):
        client.list_channels()


def test_client_not_found_404():
    session = MagicMock()
    resp = MagicMock()
    resp.status_code = 404
    resp.text = "Not Found"
    session.request.return_value = resp

    client = KnockClient(api_key="sk_valid", session=session)
    with pytest.raises(KnockNotFoundError, match="404"):
        client.get_workflow("missing-workflow")


def test_client_rate_limit_429_exhausted():
    session = MagicMock()
    resp = MagicMock()
    resp.status_code = 429
    resp.text = "Too Many Requests"
    resp.headers = {"Retry-After": "0"}
    session.request.return_value = resp

    client = KnockClient(api_key="sk_valid", session=session, max_retries=1, backoff_factor=0)
    with pytest.raises(KnockRateLimitError):
        client.list_workflows()
    assert session.request.call_count == 2


def test_client_rate_limit_429_success_after_retry():
    session = MagicMock()
    resp_fail = MagicMock(status_code=429, headers={"Retry-After": "0"}, text="Rate limit")
    resp_success = MagicMock(status_code=200)
    resp_success.json.return_value = {"entries": [{"key": "wf-1"}]}
    session.request.side_effect = [resp_fail, resp_success]

    client = KnockClient(api_key="sk_valid", session=session, max_retries=2, backoff_factor=0)
    res = client.list_workflows()
    assert res["entries"][0]["key"] == "wf-1"
    assert session.request.call_count == 2


def test_client_server_error_500_exhausted():
    session = MagicMock()
    resp = MagicMock(status_code=500, text="Internal Server Error")
    session.request.return_value = resp

    client = KnockClient(api_key="sk_valid", session=session, max_retries=1, backoff_factor=0)
    with pytest.raises(KnockServerError, match="500"):
        client.list_workflows()


def test_client_server_error_503_success_after_retry():
    session = MagicMock()
    resp_fail = MagicMock(status_code=503, text="Service Unavailable")
    resp_success = MagicMock(status_code=200)
    resp_success.json.return_value = {"entries": [{"id": "ch-1"}]}
    session.request.side_effect = [resp_fail, resp_success]

    client = KnockClient(api_key="sk_valid", session=session, max_retries=2, backoff_factor=0)
    res = client.list_channels()
    assert res["entries"][0]["id"] == "ch-1"


def test_client_network_exception_retry():
    session = MagicMock()
    session.request.side_effect = [
        requests.ConnectionError("Network down"),
        MagicMock(status_code=200, json=lambda: {"entries": []}),
    ]

    client = KnockClient(api_key="sk_valid", session=session, max_retries=2, backoff_factor=0)
    res = client.list_schedules()
    assert res == {"entries": []}


def test_client_invalid_json():
    session = MagicMock()
    resp = MagicMock(status_code=200)
    resp.json.side_effect = ValueError("Invalid JSON string")
    session.request.return_value = resp

    client = KnockClient(api_key="sk_valid", session=session)
    with pytest.raises(KnockServerError, match="invalid JSON"):
        client.list_messages()


def test_client_endpoints_dispatch():
    session = MagicMock()
    resp = MagicMock(status_code=200)
    resp.json.return_value = {"entries": []}
    session.request.return_value = resp

    client = KnockClient(api_key="sk_valid", session=session)
    client.get_workflow("wf_abc")
    assert "/workflows/wf_abc" in session.request.call_args.kwargs["url"]

    client.get_channel("ch_abc")
    assert "/channels/ch_abc" in session.request.call_args.kwargs["url"]

    client.get_schedule("sch_abc")
    assert "/schedules/sch_abc" in session.request.call_args.kwargs["url"]

    client.get_message("msg_abc")
    assert "/messages/msg_abc" in session.request.call_args.kwargs["url"]
