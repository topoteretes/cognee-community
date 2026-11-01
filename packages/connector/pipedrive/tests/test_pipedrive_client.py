"""The REST client: request shape, error mapping, retries and token hygiene."""

import pytest
import requests

from cognee_community_connector_pipedrive import pipedrive as pipedrive_module
from cognee_community_connector_pipedrive.pipedrive import (
    PipedriveAccessError,
    PipedriveAPIError,
    PipedriveAuthError,
    PipedriveClient,
    PipedriveNotFoundError,
    PipedriveRateLimitedError,
)

TOKEN = "SECRET-pipedrive-token-123"
OK = {"success": True, "data": []}


class _Response:
    def __init__(self, status: int, payload=None, headers=None):
        self.status_code = status
        self._payload = payload if payload is not None else {}
        self.headers = headers or {}

    def json(self):
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload


def _client(monkeypatch, *responses):
    calls, sleeps = [], []
    queue = list(responses)

    def fake_get(url, **kwargs):
        calls.append((url, kwargs))
        response = queue.pop(0)
        if isinstance(response, Exception):
            raise response
        return response

    monkeypatch.setattr(pipedrive_module.requests, "get", fake_get)
    return PipedriveClient(TOKEN, sleep=sleeps.append), calls, sleeps


def test_a_request_sends_the_token_header_and_params(monkeypatch):
    client, calls, _ = _client(monkeypatch, _Response(200, OK))

    assert client.request("/api/v2/deals", params={"limit": 500}) == OK

    url, kwargs = calls[0]
    assert url == "https://api.pipedrive.com/api/v2/deals"
    assert kwargs["headers"]["x-api-token"] == TOKEN
    assert kwargs["params"] == {"limit": 500}
    # the token never goes in the query string, where it would end up in logs
    assert TOKEN not in url


@pytest.mark.parametrize(
    ("status", "error"),
    [
        (401, PipedriveAuthError),
        (403, PipedriveAccessError),
        (404, PipedriveNotFoundError),
        (410, PipedriveNotFoundError),
        (400, PipedriveAPIError),
    ],
)
def test_error_statuses_map_to_errors(monkeypatch, status, error):
    client, _, _ = _client(monkeypatch, _Response(status, {"success": False}))

    with pytest.raises(error):
        client.request("/api/v2/deals/42")


def test_error_messages_carry_no_record_id(monkeypatch):
    client, _, _ = _client(monkeypatch, _Response(403))

    with pytest.raises(PipedriveAccessError) as caught:
        client.request("/api/v2/deals/987654")

    assert "987654" not in str(caught.value)
    assert "/api/v2/deals/{id}" in str(caught.value)


@pytest.mark.parametrize(
    "payload",
    [ValueError("not json"), ["a", "list"], {"success": False, "error": "nope"}, {"data": []}],
)
def test_an_unreadable_200_raises_instead_of_looking_empty(monkeypatch, payload):
    client, _, _ = _client(monkeypatch, _Response(200, payload))

    with pytest.raises(PipedriveAPIError, match="unreadable"):
        client.request("/api/v2/deals")


def test_server_errors_and_network_errors_are_retried(monkeypatch):
    client, calls, sleeps = _client(
        monkeypatch,
        _Response(502),
        requests.ConnectionError("down"),
        _Response(200, OK),
    )

    assert client.request("/v1/notes") == OK
    assert len(calls) == 3
    assert sleeps == [1, 2]


def test_a_burst_429_waits_retry_after_then_succeeds(monkeypatch):
    client, _, sleeps = _client(
        monkeypatch, _Response(429, headers={"retry-after": "2"}), _Response(200, OK)
    )

    assert client.request("/v1/notes") == OK
    assert sleeps == [2.0]


@pytest.mark.parametrize("value", ["nan", "-5", "inf"])
def test_an_odd_retry_after_falls_back_to_backoff(monkeypatch, value):
    client, _, sleeps = _client(
        monkeypatch, _Response(429, headers={"retry-after": value}), _Response(200, OK)
    )

    assert client.request("/v1/notes") == OK
    assert sleeps == [1]


def test_a_redirect_is_not_followed_and_raises(monkeypatch):
    client, calls, _ = _client(
        monkeypatch, _Response(302, headers={"location": "https://evil.example/"})
    )

    with pytest.raises(PipedriveAPIError, match="HTTP 302"):
        client.request("/v1/notes")
    assert calls[0][1]["allow_redirects"] is False


def test_waits_across_a_run_are_capped_and_reset_by_the_next_run(monkeypatch):
    burst = _Response(429, headers={"retry-after": "9"})
    client, _, sleeps = _client(monkeypatch, *([burst, _Response(200, OK)] * 6), burst)

    for _ in range(6):
        client.request("/v1/notes")
    with pytest.raises(PipedriveRateLimitedError, match="too long"):
        client.request("/v1/notes")
    assert sum(sleeps) <= 60

    client.start_run()
    monkeypatch.setattr(pipedrive_module.requests, "get", lambda url, **kw: _Response(200, OK))
    assert client.request("/v1/notes") == OK


def test_a_spent_daily_budget_stops_instead_of_sleeping_for_hours(monkeypatch):
    client, calls, sleeps = _client(monkeypatch, _Response(429, headers={"retry-after": "3600"}))

    with pytest.raises(PipedriveRateLimitedError):
        client.request("/v1/notes")
    assert sleeps == [] and len(calls) == 1


def test_a_429_after_every_retry_raises(monkeypatch):
    client, calls, _ = _client(monkeypatch, *[_Response(429)] * 3)

    with pytest.raises(PipedriveRateLimitedError):
        client.request("/v1/notes")
    assert len(calls) == 3


def test_check_active_is_asked_before_every_attempt(monkeypatch):
    client, _, _ = _client(monkeypatch, _Response(500), _Response(200, OK))
    asked = []

    client.request("/v1/notes", check_active=lambda: asked.append(1))

    assert len(asked) == 2


def test_network_errors_never_echo_the_token(monkeypatch):
    error = requests.ConnectionError(f"failed with header x-api-token: {TOKEN}")
    client, _, _ = _client(monkeypatch, error, error, error)

    with pytest.raises(PipedriveAPIError) as caught:
        client.request("/v1/notes")

    assert TOKEN not in str(caught.value)
    assert caught.value.__cause__ is None and caught.value.__suppress_context__


@pytest.mark.parametrize("token", ["", "   ", "has space", "tab\there", "new\nline", "ünï"])
def test_a_malformed_token_is_refused_without_echoing_it(token):
    with pytest.raises(ValueError) as caught:
        PipedriveClient(token)

    if token.strip():
        assert token.strip() not in str(caught.value)


def test_a_trailing_newline_from_a_paste_is_trimmed(monkeypatch):
    client, calls, _ = _client(monkeypatch, _Response(200, OK))
    client._api_token = PipedriveClient(f"{TOKEN}\n")._api_token

    client.request("/v1/users/me")

    assert calls[0][1]["headers"]["x-api-token"] == TOKEN


def test_the_client_repr_hides_the_token():
    assert TOKEN not in repr(PipedriveClient(TOKEN))
