"""Unit tests for the Egnyte HTTP client: URLs, auth header, pacing, retries and errors.

A fake ``requests``-style session replays canned responses, and ``sleep`` and
``clock`` are injected, so nothing touches the network or really waits.
"""

from urllib.parse import parse_qs, urlparse

import pytest

from cognee_community_connector_egnyte import egnyte as eg


class FakeResponse:
    def __init__(self, status_code=200, json_data=None, content=b"", headers=None, text=""):
        self.status_code = status_code
        self._json = json_data
        self.content = content
        self.headers = headers or {}
        self.text = text

    def json(self):
        return self._json


class FakeSession:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.requests = []

    def get(self, url, params=None, headers=None, timeout=None):
        self.requests.append({"url": url, "params": params, "headers": headers})
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response

    def post(self, url, data=None, timeout=None):
        self.requests.append({"url": url, "data": data})
        return self.responses.pop(0)


class FakeTime:
    def __init__(self):
        self.now = 0.0
        self.sleeps = []

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds

    def clock(self):
        return self.now


def _client(session, time=None, requests_per_second=0):
    time = time or FakeTime()
    return eg.EgnyteClient(
        "acme",
        "token-123",
        session=session,
        requests_per_second=requests_per_second,
        sleep=time.sleep,
        clock=time.clock,
    )


def _qps_throttled(retry_after="1"):
    headers = {"X-Mashery-Error-Code": "ERR_403_DEVELOPER_OVER_QPS", "Retry-After": retry_after}
    return FakeResponse(403, headers=headers)


def test_requests_use_the_domain_bearer_token_and_encoded_paths():
    session = FakeSession(FakeResponse(json_data={"files": [], "folders": []}))

    _client(session).list_folder("/Shared/Q3 Plans #2", offset=100)

    request = session.requests[0]
    assert request["url"] == "https://acme.egnyte.com/pubapi/v1/fs/Shared/Q3%20Plans%20%232"
    assert request["headers"] == {"Authorization": "Bearer token-123"}
    assert request["params"] == {"list_content": "true", "count": 100, "offset": 100}


@pytest.mark.parametrize(
    "domain", ["acme", "acme.egnyte.com", "https://acme.egnyte.com/", " acme "]
)
def test_domain_forms_resolve_to_the_same_host(domain):
    assert eg._host(domain) == "acme.egnyte.com"


def test_download_returns_raw_bytes():
    session = FakeSession(FakeResponse(content=b"hello"))

    assert _client(session).download("/Shared/a.txt") == b"hello"
    assert session.requests[0]["url"] == "https://acme.egnyte.com/pubapi/v1/fs-content/Shared/a.txt"


def test_no_new_events_is_204_no_content():
    session = FakeSession(FakeResponse(204))

    assert _client(session).list_events(41, folder="/Shared") == []
    assert session.requests[0]["params"] == {
        "id": 41,
        "folder": "/Shared",
        "type": "file_system",
        "count": 100,
    }


def test_per_second_throttle_is_retried_after_retry_after():
    time = FakeTime()
    session = FakeSession(_qps_throttled("3"), FakeResponse(json_data={"latest_event_id": 7}))

    assert _client(session, time).get_event_cursor() == {"latest_event_id": 7}
    assert time.sleeps == [3.0]


def test_server_errors_and_network_errors_are_retried():
    time = FakeTime()
    session = FakeSession(
        FakeResponse(503),
        ConnectionError("connection reset"),
        FakeResponse(json_data={"latest_event_id": 7}),
    )

    assert _client(session, time).get_event_cursor() == {"latest_event_id": 7}
    assert time.sleeps == [1, 2]


def test_retries_give_up_eventually():
    session = FakeSession(*[FakeResponse(502, text="bad gateway")] * (eg._MAX_RETRIES + 1))

    with pytest.raises(eg.EgnyteError, match="HTTP 502"):
        _client(session).get_event_cursor()


def test_daily_quota_raises_a_clear_error():
    session = FakeSession(
        FakeResponse(403, headers={"X-Mashery-Error-Code": "ERR_403_DEVELOPER_OVER_RATE"})
    )

    with pytest.raises(eg.EgnyteQuotaExceededError, match="daily call quota"):
        _client(session).get_event_cursor()


def test_not_found_and_unauthorized_are_distinguished():
    with pytest.raises(eg.EgnyteNotFoundError):
        _client(FakeSession(FakeResponse(404))).get_metadata("/Shared/missing.txt")

    with pytest.raises(eg.EgnyteError, match="HTTP 401"):
        _client(FakeSession(FakeResponse(401))).get_metadata("/Shared/a.txt")


def test_requests_are_paced_to_the_rate_limit():
    time = FakeTime()
    session = FakeSession(*[FakeResponse(json_data={}) for _ in range(3)])
    client = _client(session, time, requests_per_second=2)

    for _ in range(3):
        client.get_event_cursor()

    # 2 requests per second: the 2nd and 3rd calls each wait half a second.
    assert time.sleeps == [0.5, 0.5]


def test_get_access_token_uses_the_password_grant():
    session = FakeSession(FakeResponse(json_data={"access_token": "fresh-token"}))

    token = eg.get_access_token("acme", "key-1", "arnav", "secret", session=session)

    assert token == "fresh-token"
    assert session.requests[0] == {
        "url": "https://acme.egnyte.com/puboauth/token",
        "data": {
            "client_id": "key-1",
            "username": "arnav",
            "password": "secret",
            "grant_type": "password",
        },
    }


def test_get_access_token_reports_failures():
    session = FakeSession(FakeResponse(400, text='{"error": "invalid_grant"}'))

    with pytest.raises(eg.EgnyteError, match="invalid_grant"):
        eg.get_access_token("acme", "key-1", "arnav", "wrong", session=session)


def test_authorization_url_asks_for_a_code_with_the_registered_redirect():
    url = eg.authorization_url("acme", "client-1", "state-xyz")

    parsed = urlparse(url)
    assert f"{parsed.scheme}://{parsed.netloc}{parsed.path}" == (
        "https://acme.egnyte.com/puboauth/token"
    )
    assert parse_qs(parsed.query) == {
        "client_id": ["client-1"],
        "redirect_uri": ["https://localhost/callback"],
        "scope": ["Egnyte.filesystem"],
        "state": ["state-xyz"],
        "response_type": ["code"],
    }


def test_code_from_redirect_checks_state_and_errors():
    redirect = "https://localhost/callback?code=abc123&state=state-xyz"
    assert eg.code_from_redirect(redirect, "state-xyz") == "abc123"

    with pytest.raises(eg.EgnyteError, match="state mismatch"):
        eg.code_from_redirect(redirect, "another-state")
    with pytest.raises(eg.EgnyteError, match="not approved: access_denied"):
        eg.code_from_redirect("https://localhost/callback?error=access_denied", "state-xyz")
    with pytest.raises(eg.EgnyteError, match="no authorization code"):
        eg.code_from_redirect("https://localhost/callback?state=state-xyz", "state-xyz")


def test_exchange_code_uses_the_authorization_code_grant():
    session = FakeSession(FakeResponse(json_data={"access_token": "oauth-token"}))

    token = eg.exchange_code("acme", "client-1", "secret-1", "abc123", session=session)

    assert token == "oauth-token"
    assert session.requests[0] == {
        "url": "https://acme.egnyte.com/puboauth/token",
        "data": {
            "client_id": "client-1",
            "client_secret": "secret-1",
            "redirect_uri": "https://localhost/callback",
            "code": "abc123",
            "grant_type": "authorization_code",
        },
    }


def test_missing_credentials_are_reported_before_any_request(monkeypatch):
    for name in ("EGNYTE_ACCESS_TOKEN", "EGNYTE_API_KEY", "EGNYTE_USERNAME", "EGNYTE_PASSWORD"):
        monkeypatch.delenv(name, raising=False)

    with pytest.raises(ValueError, match="EGNYTE_ACCESS_TOKEN"):
        eg._build_client("acme", None, None, None, None, requests_per_second=2)
