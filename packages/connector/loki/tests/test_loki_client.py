"""The HTTP client: auth, tenant header, retries and error mapping, with requests faked."""

import pytest
import requests

from cognee_community_connector_loki.loki import (
    LokiAPIError,
    LokiAuthError,
    LokiClient,
    LokiQueryError,
)

STREAMS = [{"stream": {"service_name": "checkout"}, "values": [["1", "boom"]]}]


class FakeResponse:
    def __init__(self, status=200, payload=None, text=""):
        self.status_code = status
        self._payload = payload
        self.text = text

    def json(self):
        if self._payload is None:
            raise ValueError("no json")
        return self._payload


def ok(result=STREAMS, result_type="streams"):
    return FakeResponse(
        payload={"status": "success", "data": {"resultType": result_type, "result": result}}
    )


class FakeSession:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


def client(session, **kwargs):
    return LokiClient("http://loki:3100/", session=session, sleep=lambda _: None, **kwargs)


def test_query_range_sends_the_window_and_returns_streams():
    session = FakeSession(ok())

    streams = client(session).query_range('{service_name="checkout"}', 10, 20, 500)

    url, kwargs = session.calls[0]
    assert url == "http://loki:3100/loki/api/v1/query_range"
    assert kwargs["params"] == {
        "query": '{service_name="checkout"}',
        "start": "10",
        "end": "20",
        "limit": "500",
        "direction": "forward",
    }
    assert kwargs["auth"] is None
    assert kwargs["headers"]["X-Loki-Response-Encoding-Flags"] == "categorize-labels"
    assert streams == STREAMS


def test_basic_auth_and_tenant_header():
    session = FakeSession(ok())

    client(session, username="123456", password="glc_x", tenant_id="team-a").query_range(
        "{a}", 0, 1, 1
    )

    kwargs = session.calls[0][1]
    assert kwargs["auth"] == ("123456", "glc_x")
    assert kwargs["headers"]["X-Scope-OrgID"] == "team-a"
    assert "Authorization" not in kwargs["headers"]


def test_bearer_token():
    session = FakeSession(ok())

    client(session, token="secret").query_range("{a}", 0, 1, 1)

    assert session.calls[0][1]["headers"]["Authorization"] == "Bearer secret"


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"token": "t", "username": "u", "password": "p"}, "not both"),
        ({"username": "u"}, "both a username and a password"),
    ],
)
def test_conflicting_auth_is_rejected(kwargs, message):
    with pytest.raises(ValueError, match=message):
        client(FakeSession(), **kwargs)


def test_a_base_url_without_a_scheme_is_rejected():
    with pytest.raises(ValueError, match="http"):
        LokiClient("loki:3100")


def test_the_repr_hides_credentials():
    assert "secret" not in repr(client(FakeSession(), token="secret"))


def test_server_errors_and_rate_limits_are_retried():
    session = FakeSession(FakeResponse(503), FakeResponse(429), ok())

    assert client(session).query_range("{a}", 0, 1, 1) == STREAMS
    assert len(session.calls) == 3


def test_network_errors_are_retried_then_reported_without_details():
    session = FakeSession(*[requests.ConnectionError("http://user:pw@loki")] * 3)

    with pytest.raises(LokiAPIError) as raised:
        client(session).query_range("{a}", 0, 1, 1)

    assert "pw" not in str(raised.value)
    assert "ConnectionError" in str(raised.value)


@pytest.mark.parametrize("status", [401, 403])
def test_rejected_credentials(status):
    with pytest.raises(LokiAuthError):
        client(FakeSession(FakeResponse(status))).query_range("{a}", 0, 1, 1)


def test_a_rejected_query_carries_lokis_reason():
    response = FakeResponse(400, text="parse error at line 1, col 2: syntax error")

    with pytest.raises(LokiQueryError, match="parse error"):
        client(FakeSession(response)).query_range("{a", 0, 1, 1)


def test_a_metric_query_is_rejected():
    with pytest.raises(LokiQueryError, match="log query"):
        client(FakeSession(ok(result=[], result_type="matrix"))).query_range(
            "rate({a}[1m])", 0, 1, 1
        )


@pytest.mark.parametrize(
    "response",
    [FakeResponse(200), FakeResponse(200, payload={"status": "error"}), FakeResponse(418)],
)
def test_unreadable_answers(response):
    with pytest.raises(LokiAPIError):
        client(FakeSession(response)).query_range("{a}", 0, 1, 1)
