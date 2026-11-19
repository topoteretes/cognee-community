"""The REST client: request shape, error mapping, retries and credential hygiene."""

import pytest
import requests

from cognee_community_connector_amplitude import amplitude as amplitude_module
from cognee_community_connector_amplitude.amplitude import (
    AmplitudeAccessError,
    AmplitudeAPIError,
    AmplitudeAuthError,
    AmplitudeClient,
    AmplitudeNotFoundError,
    AmplitudeRateLimitedError,
)

KEY = "SECRET-amplitude-key-123"
SECRET = "SECRET-amplitude-secret-456"


def _error(details: str) -> dict:
    return {"error": {"http_code": 403, "type": "unspecified", "metadata": {"details": details}}}


class _Response:
    def __init__(self, status: int, payload=None):
        self.status_code = status
        self._payload = payload if payload is not None else {}

    def json(self):
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload


def _client(monkeypatch, *responses, region="us"):
    calls, sleeps = [], []
    queue = list(responses)

    def fake_get(url, **kwargs):
        calls.append((url, kwargs))
        response = queue.pop(0)
        if isinstance(response, Exception):
            raise response
        return response

    monkeypatch.setattr(amplitude_module.requests, "get", fake_get)
    return AmplitudeClient(KEY, SECRET, region=region, sleep=sleeps.append), calls, sleeps


def test_a_request_sends_basic_auth_and_query_params(monkeypatch):
    client, calls, _ = _client(monkeypatch, _Response(200, {"success": True, "data": []}))

    payload = client.get("/api/2/taxonomy/event-property", {"event_type": "Checkout Completed"})

    assert payload == {"success": True, "data": []}
    url, kwargs = calls[0]
    assert url == "https://amplitude.com/api/2/taxonomy/event-property"
    assert kwargs["auth"] == (KEY, SECRET)
    assert kwargs["params"] == {"event_type": "Checkout Completed"}


def test_the_eu_region_uses_the_eu_host(monkeypatch):
    client, calls, _ = _client(monkeypatch, _Response(200, {"cohorts": []}), region="eu")

    client.get("/api/3/cohorts")

    assert calls[0][0] == "https://analytics.eu.amplitude.com/api/3/cohorts"


def test_an_unknown_region_is_rejected():
    with pytest.raises(ValueError, match="region"):
        AmplitudeClient(KEY, SECRET, region="apac")


@pytest.mark.parametrize(
    ("status", "payload", "error"),
    [
        (401, {}, AmplitudeAuthError),
        # amplitude answers 403 to a wrong key, secret or region
        (403, _error("Invalid API Key"), AmplitudeAuthError),
        (403, _error("Invalid API/Secret Key combination"), AmplitudeAuthError),
        (403, _error("Taxonomy is not available on your plan"), AmplitudeAccessError),
        (403, ValueError("not json"), AmplitudeAccessError),
        (429, {}, AmplitudeRateLimitedError),
        (400, {"success": False, "errors": [{"message": "Not found"}]}, AmplitudeAPIError),
        (404, {"error": {"http_code": 404}}, AmplitudeNotFoundError),
        (404, ValueError("html page"), AmplitudeNotFoundError),
        (200, ["not", "an", "object"], AmplitudeAPIError),
        (200, ValueError("html page"), AmplitudeAPIError),
    ],
)
def test_error_statuses_map_to_typed_errors(monkeypatch, status, payload, error):
    client, _, _ = _client(monkeypatch, _Response(status, payload))

    with pytest.raises(error) as raised:
        client.get("/api/3/cohorts")

    assert type(raised.value) is error
    assert KEY not in str(raised.value)
    assert SECRET not in str(raised.value)


def test_a_plan_403_names_the_endpoint_the_key_cannot_call(monkeypatch):
    client, _, _ = _client(monkeypatch, _Response(403))

    with pytest.raises(AmplitudeAccessError, match="/api/2/taxonomy/event"):
        client.get("/api/2/taxonomy/event")


def test_a_credentials_403_points_at_the_region(monkeypatch):
    client, _, _ = _client(monkeypatch, _Response(403, _error("Invalid API Key")))

    with pytest.raises(AmplitudeAuthError, match="region"):
        client.get("/api/3/cohorts")


def test_server_errors_and_network_failures_are_retried(monkeypatch):
    client, calls, sleeps = _client(
        monkeypatch,
        _Response(503),
        requests.ConnectionError("boom"),
        _Response(200, {"cohorts": []}),
    )

    assert client.get("/api/3/cohorts") == {"cohorts": []}
    assert len(calls) == 3
    assert sleeps == [1, 2]


def test_a_persistent_server_error_raises(monkeypatch):
    client, calls, _ = _client(monkeypatch, *[_Response(502)] * 3)

    with pytest.raises(AmplitudeAPIError, match="HTTP 502"):
        client.get("/api/3/cohorts")

    assert len(calls) == 3


def test_a_persistent_network_failure_raises_without_the_credentials(monkeypatch):
    client, _, _ = _client(monkeypatch, *[requests.ConnectionError(KEY + SECRET)] * 3)

    with pytest.raises(AmplitudeAPIError) as raised:
        client.get("/api/3/cohorts")

    assert KEY not in str(raised.value)
    assert SECRET not in str(raised.value)
    assert raised.value.__cause__ is None


def test_the_credentials_are_never_shown():
    shown = repr(AmplitudeClient(KEY, SECRET))

    assert KEY not in shown
    assert SECRET not in shown


@pytest.mark.parametrize("bad", ["", "   ", "has space", "tab\tkey", "ключ"])
def test_malformed_credentials_are_rejected_without_echoing_them(bad):
    for kwargs in ({"api_key": bad, "secret_key": SECRET}, {"api_key": KEY, "secret_key": bad}):
        with pytest.raises(ValueError) as raised:
            AmplitudeClient(**kwargs)

        assert not bad.strip() or bad not in str(raised.value)
