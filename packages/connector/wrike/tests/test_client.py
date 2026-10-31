"""WrikeClient and OAuth refresh, against httpx.MockTransport (no network)."""

import json
import stat

import httpx
import pytest

from cognee_community_connector_wrike import wrike
from cognee_community_connector_wrike.wrike import (
    WrikeAPIError,
    WrikeAuthError,
    WrikeClient,
    refresh_access_token,
)

TOKEN = "secret-token-123"


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    slept = []
    monkeypatch.setattr(wrike.time, "sleep", slept.append)
    return slept


def client_for(handler, host="www.wrike.com"):
    return WrikeClient(TOKEN, host=host, http=httpx.Client(transport=httpx.MockTransport(handler)))


def test_get_follows_next_page_token_and_encodes_params():
    seen = []

    def handler(request):
        seen.append(request.url)
        if "nextPageToken" not in request.url.params:
            return httpx.Response(200, json={"data": [{"id": "1"}], "nextPageToken": "p2"})
        return httpx.Response(200, json={"data": [{"id": "2"}]})

    items = client_for(handler).get("/tasks", pageSize=1000, fields=["description"])

    assert [i["id"] for i in items] == ["1", "2"]
    assert seen[0].host == "www.wrike.com" and seen[0].path == "/api/v4/tasks"
    # Lists go as JSON, and the original params are repeated on the next page.
    assert seen[1].params["fields"] == '["description"]'
    assert seen[1].params["pageSize"] == "1000"
    assert seen[1].params["nextPageToken"] == "p2"


def test_sends_bearer_token_to_configured_host():
    def handler(request):
        assert request.headers["Authorization"] == f"bearer {TOKEN}"
        assert request.url.host == "app-eu.wrike.com"
        return httpx.Response(200, json={"data": []})

    assert client_for(handler, host="app-eu.wrike.com").get("/folders") == []


def test_retries_429_using_retry_after(no_sleep):
    calls = iter(
        [httpx.Response(429, headers={"Retry-After": "7"}), httpx.Response(200, json={"data": []})]
    )
    assert client_for(lambda request: next(calls)).get("/tasks") == []
    assert no_sleep == [7.0]


def test_gives_up_after_max_retries():
    with pytest.raises(WrikeAPIError) as err:
        client_for(lambda request: httpx.Response(503)).get("/tasks")
    assert err.value.status == 503


def test_401_raises_auth_error_without_leaking_token():
    def handler(request):
        return httpx.Response(401, json={"error": "not_authorized", "errorDescription": "x"})

    with pytest.raises(WrikeAuthError) as err:
        client_for(handler).get("/tasks")
    assert err.value.code == "not_authorized"
    assert TOKEN not in str(err.value)


def test_page_token_that_does_not_advance_raises():
    def handler(request):
        return httpx.Response(200, json={"data": [], "nextPageToken": "same"})

    with pytest.raises(WrikeAPIError, match="same nextPageToken"):
        client_for(handler).get("/tasks")


@pytest.mark.parametrize("host", ["evil.com", "wrike.com.evil.com", ""])
def test_rejects_non_wrike_host(host):
    with pytest.raises(ValueError):
        WrikeClient(TOKEN, host=host)


@pytest.mark.parametrize("token", ["", "   ", "has space", "nön-ascii"])
def test_rejects_malformed_token_without_echoing_it(token):
    with pytest.raises(ValueError) as err:
        WrikeClient(token)
    assert not token.strip() or token not in str(err.value)


def test_refresh_saves_rotated_token_before_returning(tmp_path):
    token_file = tmp_path / "wrike_tokens.json"
    token_file.write_text(json.dumps({"refresh_token": "r0", "access_token": "a0"}))

    def handler(request):
        form = dict(httpx.QueryParams(request.content.decode()))
        assert form["grant_type"] == "refresh_token" and form["refresh_token"] == "r0"
        body = {"access_token": "a1", "refresh_token": "r1", "host": "app-eu.wrike.com"}
        return httpx.Response(200, json=body)

    http = httpx.Client(transport=httpx.MockTransport(handler))
    access, host = refresh_access_token("id", "secret", token_file, http=http)

    assert (access, host) == ("a1", "app-eu.wrike.com")
    assert json.loads(token_file.read_text())["refresh_token"] == "r1"
    assert stat.S_IMODE(token_file.stat().st_mode) == 0o600
    assert [p.name for p in tmp_path.iterdir()] == ["wrike_tokens.json"]  # no temp files left


def test_failed_refresh_leaves_token_file_untouched(tmp_path):
    token_file = tmp_path / "wrike_tokens.json"
    original = json.dumps({"refresh_token": "r0"})
    token_file.write_text(original)

    def handler(request):
        return httpx.Response(400, json={"error": "invalid_grant"})

    http = httpx.Client(transport=httpx.MockTransport(handler))
    with pytest.raises(WrikeAuthError) as err:
        refresh_access_token("id", "secret", token_file, http=http)

    assert err.value.code == "invalid_grant"
    assert token_file.read_text() == original


def test_refresh_without_token_file_is_a_clear_error(tmp_path):
    with pytest.raises(WrikeAuthError, match="refresh_token"):
        refresh_access_token("id", "secret", tmp_path / "missing.json")
