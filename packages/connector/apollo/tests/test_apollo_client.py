"""The REST client: request shape, error mapping, retries and key hygiene."""

import pytest
import requests

from cognee_community_connector_apollo import apollo as apollo_module
from cognee_community_connector_apollo.apollo import (
    ApolloAccessError,
    ApolloAPIError,
    ApolloAuthError,
    ApolloClient,
    ApolloNotFoundError,
    ApolloRateLimitedError,
)

KEY = "SECRET-apollo-key-123"


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

    def fake_request(method, url, **kwargs):
        calls.append((method, url, kwargs))
        response = queue.pop(0)
        if isinstance(response, Exception):
            raise response
        return response

    monkeypatch.setattr(apollo_module.requests, "request", fake_request)
    return ApolloClient(KEY, sleep=sleeps.append), calls, sleeps


def test_a_request_sends_the_key_header_and_json_body(monkeypatch):
    headers = {"x-hourly-requests-left": "399", "x-24-hour-requests-left": "1999"}
    client, calls, _ = _client(monkeypatch, _Response(200, {"contacts": []}, headers))

    assert client.request("POST", "/contacts/search", body={"page": 1}) == {"contacts": []}

    method, url, kwargs = calls[0]
    assert (method, url) == ("POST", "https://api.apollo.io/api/v1/contacts/search")
    assert kwargs["headers"]["x-api-key"] == KEY
    assert kwargs["json"] == {"page": 1}
    assert client.rate_limit == {"hourly": 399, "daily": 1999}


@pytest.mark.parametrize(
    ("status", "payload", "error"),
    [
        (401, {}, ApolloAuthError),
        (403, {"error_code": "API_INACCESSIBLE"}, ApolloAccessError),
        (429, {}, ApolloRateLimitedError),
        (404, {"error": "Contact not found"}, ApolloNotFoundError),
        (422, {"error": "This contact has been deleted."}, ApolloNotFoundError),
        (422, {"error": "Oops! This contact does not exist in Apollo."}, ApolloNotFoundError),
        (422, {"error": "This account has been deleted!"}, ApolloNotFoundError),
        (422, {"error": "Page * per page number is over threshold."}, ApolloAPIError),
        (400, {}, ApolloAPIError),
    ],
)
def test_error_statuses_map_to_typed_errors(monkeypatch, status, payload, error):
    client, _, _ = _client(monkeypatch, _Response(status, payload))

    with pytest.raises(error) as raised:
        client.request("GET", "/contacts/c1")

    assert type(raised.value) is error
    assert KEY not in str(raised.value)


def test_a_403_names_the_endpoint_the_key_cannot_call(monkeypatch):
    client, _, _ = _client(monkeypatch, _Response(403))

    with pytest.raises(ApolloAccessError, match="/emailer_campaigns/activity_feed"):
        client.request("POST", "/emailer_campaigns/activity_feed", body={})


def test_server_errors_and_network_failures_are_retried(monkeypatch):
    client, calls, sleeps = _client(
        monkeypatch,
        _Response(503),
        requests.ConnectionError("boom"),
        _Response(200, {"ok": True}),
    )

    assert client.request("GET", "/labels") == {"ok": True}
    assert len(calls) == 3
    assert sleeps == [1, 2]


def test_a_persistent_network_failure_raises_without_the_key(monkeypatch):
    client, _, _ = _client(monkeypatch, *[requests.ConnectionError(KEY)] * 3)

    with pytest.raises(ApolloAPIError) as raised:
        client.request("GET", "/labels")

    assert KEY not in str(raised.value)
    assert raised.value.__cause__ is None


def test_the_key_is_never_shown():
    assert KEY not in repr(ApolloClient(KEY))


@pytest.mark.parametrize("bad", ["", "   ", "has space", "tab\tkey", "ключ"])
def test_a_malformed_key_is_rejected_without_echoing_it(bad):
    with pytest.raises(ValueError) as raised:
        ApolloClient(bad)

    assert not bad.strip() or bad not in str(raised.value)
