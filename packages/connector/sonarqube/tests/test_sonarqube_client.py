"""The retrying HTTP client: backoff, wait budget, error messages, check_active."""

import pytest
from fake_sonarqube import FakeSonarQubeSession, error_body

import cognee_community_connector_sonarqube.sonarqube as module
from cognee_community_connector_sonarqube.sonarqube import (
    SonarQubeAPIError,
    _Client,
    _get_json,
    _NotFoundError,
)


def _client(session, **kwargs):
    kwargs.setdefault("sleep", lambda seconds: None)
    return _Client(session, "https://sonarqube.example.com", 30.0, **kwargs)


def test_retries_on_429_then_succeeds():
    session = FakeSonarQubeSession(projects={"proj": {"name": "Proj"}})
    session.script("/api/components/show", 429, error_body("rate limited"), {"Retry-After": "1"})
    client = _client(session)
    body = _get_json(client, "/api/components/show", {"component": "proj"})
    assert body["component"]["name"] == "Proj"


def test_retries_on_502_503_504():
    session = FakeSonarQubeSession(projects={"proj": {"name": "Proj"}})
    for status in (502, 503, 504):
        session.script("/api/components/show", status, error_body("down"))
    client = _client(session)
    body = _get_json(client, "/api/components/show", {"component": "proj"})
    assert body["component"]["name"] == "Proj"


def test_exhausts_retries_and_raises():
    session = FakeSonarQubeSession(projects={"proj": {"name": "Proj"}})
    for _ in range(10):
        session.script("/api/components/show", 503, error_body("down"))
    client = _client(session)
    with pytest.raises(SonarQubeAPIError):
        _get_json(client, "/api/components/show", {"component": "proj"})


def test_401_message_has_token_hint_and_no_token():
    session = FakeSonarQubeSession(projects={})
    session.script("/api/components/show", 401, error_body("Unauthorized"))
    client = _client(session)
    with pytest.raises(SonarQubeAPIError) as exc_info:
        _get_json(client, "/api/components/show", {"component": "proj"})
    message = str(exc_info.value)
    assert "Generate a user token" in message
    assert "401" in message


def test_403_message_names_project_and_permission():
    session = FakeSonarQubeSession(projects={})
    session.script("/api/components/show", 403, error_body("Insufficient privileges"))
    client = _client(session)
    with pytest.raises(SonarQubeAPIError) as exc_info:
        _get_json(client, "/api/components/show", {"component": "proj"}, project="proj")
    message = str(exc_info.value)
    assert "proj" in message
    assert "Browse" in message


def test_404_raises_not_found_error_naming_project():
    session = FakeSonarQubeSession(projects={})
    client = _client(session)
    with pytest.raises(_NotFoundError) as exc_info:
        _get_json(client, "/api/components/show", {"component": "ghost"}, project="ghost")
    assert "ghost" in str(exc_info.value)


def test_error_message_includes_server_msg():
    session = FakeSonarQubeSession(projects={})
    session.script("/api/components/show", 403, error_body("Insufficient privileges on project"))
    client = _client(session)
    with pytest.raises(SonarQubeAPIError) as exc_info:
        _get_json(client, "/api/components/show", {"component": "proj"}, project="proj")
    assert "Insufficient privileges" in str(exc_info.value)


def test_check_active_called_before_request():
    calls = []
    session = FakeSonarQubeSession(projects={"proj": {"name": "Proj"}})
    client = _client(session, check_active=lambda: calls.append("checked"))
    _get_json(client, "/api/components/show", {"component": "proj"})
    assert calls == ["checked"]


def test_check_active_can_stop_the_run():
    def boom():
        raise RuntimeError("stopped")

    session = FakeSonarQubeSession(projects={"proj": {"name": "Proj"}})
    client = _client(session, check_active=boom)
    with pytest.raises(RuntimeError, match="stopped"):
        _get_json(client, "/api/components/show", {"component": "proj"})


def test_wait_budget_is_capped(monkeypatch):
    session = FakeSonarQubeSession(projects={"proj": {"name": "Proj"}})
    for _ in range(5):
        session.script("/api/components/show", 503, error_body("down"))
    slept = []
    client = _client(session, sleep=slept.append)
    with pytest.raises(SonarQubeAPIError):
        _get_json(client, "/api/components/show", {"component": "proj"})
    assert sum(slept) <= module._MAX_TOTAL_WAIT_SECONDS


def test_connection_error_never_echoes_exception_text():
    class ExplodingSession:
        def get(self, *args, **kwargs):
            raise ConnectionError("tcp failed to host with secret=abc123")

    client = _client(ExplodingSession(), sleep=lambda s: None)
    with pytest.raises(SonarQubeAPIError) as exc_info:
        _get_json(client, "/api/components/show", {"component": "proj"})
    assert "secret=abc123" not in str(exc_info.value)
