"""The REST client: request shape, error mapping, retries and key hygiene."""

import pytest
import requests

from cognee_community_connector_copper import copper as copper_module
from cognee_community_connector_copper.copper import (
    CopperAccessError,
    CopperAPIError,
    CopperAuthError,
    CopperClient,
    CopperNotFoundError,
    CopperRateLimitedError,
)

KEY = "SECRET-copper-key-123"
EMAIL = "owner@acme.example"
OK: list = []


class _Response:
    def __init__(self, status: int, payload=None, headers=None):
        self.status_code = status
        self._payload = payload if payload is not None else []
        self.headers = headers or {}

    def json(self):
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload


def _client(monkeypatch, *responses):
    calls, sleeps = [], []
    queue = list(responses)

    def fake_request(method, url, **kwargs):
        calls.append((method, url, kwargs))
        response = queue.pop(0)
        if isinstance(response, Exception):
            raise response
        return response

    monkeypatch.setattr(copper_module.requests, "request", fake_request)
    return CopperClient(KEY, EMAIL, sleep=sleeps.append), calls, sleeps


def test_a_request_sends_the_key_email_and_json_body(monkeypatch):
    client, calls, _ = _client(monkeypatch, _Response(200, OK))

    body = {"page_size": 200, "page_number": 1}
    assert client.request("POST", "/people/search", body=body) == OK

    method, url, kwargs = calls[0]
    assert (method, url) == ("POST", "https://api.copper.com/developer_api/v1/people/search")
    assert kwargs["headers"]["X-PW-AccessToken"] == KEY
    assert kwargs["headers"]["X-PW-UserEmail"] == EMAIL
    assert kwargs["headers"]["X-PW-Application"] == "developer_api"
    assert kwargs["headers"]["Content-Type"] == "application/json"
    assert kwargs["json"] == body
    # the key never goes in the url, where it would end up in logs
    assert KEY not in url


def test_a_single_record_answer_is_an_object(monkeypatch):
    client, _, _ = _client(monkeypatch, _Response(200, {"id": 7}))

    assert client.request("GET", "/people/7") == {"id": 7}


@pytest.mark.parametrize(
    ("status", "error"),
    [
        (401, CopperAuthError),
        (403, CopperAccessError),
        (404, CopperNotFoundError),
        (410, CopperNotFoundError),
        (400, CopperAPIError),
    ],
)
def test_error_statuses_map_to_errors(monkeypatch, status, error):
    client, _, _ = _client(monkeypatch, _Response(status, {"message": "nope"}))

    with pytest.raises(error):
        client.request("GET", "/people/42")


def test_a_401_explains_that_the_email_must_match_the_key(monkeypatch):
    client, _, _ = _client(monkeypatch, _Response(401))

    with pytest.raises(CopperAuthError, match="user who created the key"):
        client.request("GET", "/account")


def test_error_messages_carry_no_record_id(monkeypatch):
    client, _, _ = _client(monkeypatch, _Response(403))

    with pytest.raises(CopperAccessError) as caught:
        client.request("GET", "/opportunities/987654")

    assert "987654" not in str(caught.value)
    assert "/opportunities/{id}" in str(caught.value)


@pytest.mark.parametrize("payload", [ValueError("not json"), "a string", 42])
def test_an_unreadable_200_raises_instead_of_looking_empty(monkeypatch, payload):
    client, _, _ = _client(monkeypatch, _Response(200, payload))

    with pytest.raises(CopperAPIError, match="unreadable"):
        client.request("POST", "/people/search")


def test_server_errors_and_network_errors_are_retried(monkeypatch):
    client, calls, sleeps = _client(
        monkeypatch,
        _Response(502),
        requests.ConnectionError("down"),
        _Response(200, OK),
    )

    assert client.request("POST", "/people/search") == OK
    assert len(calls) == 3
    assert sleeps == [1, 2]


def test_a_429_waits_retry_after_then_succeeds(monkeypatch):
    client, _, sleeps = _client(
        monkeypatch, _Response(429, headers={"retry-after": "20"}), _Response(200, OK)
    )

    assert client.request("POST", "/people/search") == OK
    assert sleeps == [20.0]


@pytest.mark.parametrize("value", ["nan", "-5", "inf"])
def test_an_odd_retry_after_falls_back_to_backoff(monkeypatch, value):
    client, _, sleeps = _client(
        monkeypatch, _Response(429, headers={"retry-after": value}), _Response(200, OK)
    )

    assert client.request("POST", "/people/search") == OK
    assert sleeps == [4]


def test_a_long_retry_after_stops_instead_of_sleeping(monkeypatch):
    client, calls, sleeps = _client(monkeypatch, _Response(429, headers={"retry-after": "3600"}))

    with pytest.raises(CopperRateLimitedError):
        client.request("POST", "/people/search")
    assert sleeps == [] and len(calls) == 1


def test_a_429_after_every_retry_raises(monkeypatch):
    client, calls, _ = _client(monkeypatch, *[_Response(429)] * 3)

    with pytest.raises(CopperRateLimitedError):
        client.request("POST", "/people/search")
    assert len(calls) == 3


def test_a_redirect_is_not_followed_and_raises(monkeypatch):
    client, calls, _ = _client(
        monkeypatch, _Response(302, headers={"location": "https://evil.example/"})
    )

    with pytest.raises(CopperAPIError, match="HTTP 302"):
        client.request("GET", "/account")
    assert calls[0][2]["allow_redirects"] is False


def test_waits_across_a_run_are_capped_and_reset_by_the_next_run(monkeypatch):
    burst = _Response(429, headers={"retry-after": "50"})
    client, _, sleeps = _client(monkeypatch, burst, _Response(200, OK), burst, _Response(200, OK))

    client.request("POST", "/people/search")
    client.request("POST", "/people/search")
    monkeypatch.setattr(copper_module.requests, "request", lambda *a, **kw: burst)
    with pytest.raises(CopperRateLimitedError, match="too long"):
        client.request("POST", "/people/search")
    assert sum(sleeps) <= 120

    client.start_run()
    monkeypatch.setattr(copper_module.requests, "request", lambda *a, **kw: _Response(200, OK))
    assert client.request("POST", "/people/search") == OK


def test_check_active_is_asked_before_every_attempt(monkeypatch):
    client, _, _ = _client(monkeypatch, _Response(500), _Response(200, OK))
    asked = []

    client.request("GET", "/account", check_active=lambda: asked.append(1))

    assert len(asked) == 2


def test_network_errors_never_echo_the_key(monkeypatch):
    error = requests.ConnectionError(f"failed with header X-PW-AccessToken: {KEY}")
    client, _, _ = _client(monkeypatch, error, error, error)

    with pytest.raises(CopperAPIError) as caught:
        client.request("GET", "/account")

    assert KEY not in str(caught.value)
    assert caught.value.__cause__ is None and caught.value.__suppress_context__


@pytest.mark.parametrize("key", ["", "   ", "has space", "tab\there", "new\nline", "ünï"])
def test_a_malformed_key_is_refused_without_echoing_it(key):
    with pytest.raises(ValueError) as caught:
        CopperClient(key, EMAIL)

    if key.strip():
        assert key.strip() not in str(caught.value)


@pytest.mark.parametrize("email", ["", "not-an-email", "a b@c.example"])
def test_a_missing_or_malformed_email_is_refused(email):
    with pytest.raises(ValueError, match="user email"):
        CopperClient(KEY, email)


def test_a_trailing_newline_from_a_paste_is_trimmed(monkeypatch):
    client, calls, _ = _client(monkeypatch, _Response(200, OK))
    client._api_key = CopperClient(f"{KEY}\n", f"{EMAIL}\n")._api_key

    client.request("GET", "/account")

    assert calls[0][2]["headers"]["X-PW-AccessToken"] == KEY


def test_the_client_repr_hides_the_key_and_email():
    text = repr(CopperClient(KEY, EMAIL))
    assert KEY not in text and EMAIL not in text
