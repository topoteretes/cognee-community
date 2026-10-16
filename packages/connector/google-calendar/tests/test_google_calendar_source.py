"""google_calendar_source() factory, settings and the Google sign-in builders."""

import os
import stat

import pytest
from cognee.modules.integrations.google import GoogleAuthError, GoogleSettings
from cognee.tasks.ingestion.dlt_utils import DOCUMENT_SOURCE_ATTR, PIPELINE_SCOPE_ATTR
from fake_calendar import FakeCalendar

import cognee_community_connector_google_calendar.google_calendar as module
from cognee_community_connector_google_calendar import (
    GoogleCalendarSettings,
    build_calendar_service,
    google_calendar_source,
    list_calendars,
)

ENV = (
    "GOOGLE_CALENDAR_CLIENT_ID",
    "GOOGLE_CALENDAR_CLIENT_SECRET",
    "GOOGLE_CALENDAR_REFRESH_TOKEN",
    "GOOGLE_CALENDAR_CREDENTIALS_PATH",
    "GOOGLE_CALENDAR_TOKEN_PATH",
    "GOOGLE_CALENDAR_ACCESS_TOKEN",
    "GOOGLE_CALENDAR_IDS",
)


@pytest.fixture(autouse=True)
def no_calendar_env(monkeypatch):
    for name in ENV:
        monkeypatch.delenv(name, raising=False)


# ---------------------------------------------------------------------------
# Settings inherited from cognee
# ---------------------------------------------------------------------------
def test_settings_inherit_cognee_google_settings_and_read_their_prefix(monkeypatch):
    monkeypatch.setenv("GOOGLE_CALENDAR_CLIENT_ID", "client-123")
    monkeypatch.setenv("GOOGLE_CALENDAR_REFRESH_TOKEN", "refresh-abc")

    settings = GoogleCalendarSettings()

    assert isinstance(settings, GoogleSettings)
    assert settings.client_id == "client-123"
    assert settings.refresh_token == "refresh-abc"


def test_refresh_token_without_client_credentials_raises_cognee_auth_error():
    settings = GoogleCalendarSettings(refresh_token="refresh-abc")

    with pytest.raises(GoogleAuthError, match="missing_client_credentials"):
        module._build_service_from_refresh_token(settings)


# ---------------------------------------------------------------------------
# Installed-app flow
# ---------------------------------------------------------------------------
class _FakeCreds:
    valid = True
    expired = False
    refresh_token = "r"

    def to_json(self):
        return '{"token": "t"}'


def _fake_libraries(calls):
    class Flow:
        @classmethod
        def from_client_secrets_file(cls, path, scopes):
            calls["scopes"] = scopes
            return cls()

        def run_local_server(self, port):
            calls["browser"] = True
            return _FakeCreds()

    class Credentials:
        @staticmethod
        def from_authorized_user_file(path, scopes):
            return _FakeCreds()

    def build(name, version, credentials, cache_discovery):
        calls["built"] = (name, version)
        return "service"

    return lambda: (object, Credentials, Flow, build)


def test_first_login_writes_a_private_token_file_with_only_the_readonly_scope(
    tmp_path, monkeypatch
):
    calls: dict = {}
    monkeypatch.setattr(module, "_google_libraries", _fake_libraries(calls))
    secrets = tmp_path / "client_secret.json"
    secrets.write_text("{}")
    token = tmp_path / "token.json"

    service = build_calendar_service(str(secrets), str(token))

    assert service == "service"
    assert calls["built"] == ("calendar", "v3")
    assert calls["scopes"] == ["https://www.googleapis.com/auth/calendar.readonly"]
    assert calls["browser"] is True
    assert stat.S_IMODE(os.stat(token).st_mode) == 0o600


def test_cached_token_skips_the_browser(tmp_path, monkeypatch):
    calls: dict = {}
    monkeypatch.setattr(module, "_google_libraries", _fake_libraries(calls))
    token = tmp_path / "token.json"
    token.write_text("{}")

    build_calendar_service(str(tmp_path / "missing.json"), str(token))

    assert "browser" not in calls


def test_missing_client_secrets_file_is_a_clear_error(tmp_path, monkeypatch):
    monkeypatch.setattr(module, "_google_libraries", _fake_libraries({}))

    with pytest.raises(FileNotFoundError, match="GOOGLE_CALENDAR_CREDENTIALS_PATH"):
        build_calendar_service(str(tmp_path / "nope.json"), str(tmp_path / "token.json"))


# ---------------------------------------------------------------------------
# google_calendar_source()
# ---------------------------------------------------------------------------
def test_resource_opts_into_document_mode_and_pipeline_scope():
    resource = google_calendar_source(service=FakeCalendar(), resource_name="work_calendar")

    assert resource.name == "work_calendar"
    assert getattr(resource, DOCUMENT_SOURCE_ATTR) == "google_calendar"
    assert getattr(resource, PIPELINE_SCOPE_ATTR) == "work_calendar"
    assert resource.cognee_sync_stats == {}


def test_missing_credentials_fail_when_the_source_is_built():
    with pytest.raises(ValueError, match="credentials are missing"):
        google_calendar_source()


@pytest.mark.parametrize(
    "env",
    [
        {"GOOGLE_CALENDAR_ACCESS_TOKEN": "t"},
        {"GOOGLE_CALENDAR_REFRESH_TOKEN": "r"},
        {"GOOGLE_CALENDAR_CREDENTIALS_PATH": "/tmp/client.json"},
    ],
)
def test_any_one_auth_method_from_the_environment_is_enough(monkeypatch, env):
    for name, value in env.items():
        monkeypatch.setenv(name, value)

    google_calendar_source()  # builds without contacting Google


def test_empty_calendar_list_is_refused_instead_of_forgetting_everything():
    with pytest.raises(ValueError, match="calendar_ids is empty"):
        google_calendar_source([], service=FakeCalendar())


def test_calendar_ids_come_from_the_environment_and_are_deduplicated(monkeypatch):
    monkeypatch.setenv("GOOGLE_CALENDAR_IDS", " primary , team@group.calendar.google.com,primary")

    assert module._normalize_calendar_ids(None) == ("primary", "team@group.calendar.google.com")
    assert module._normalize_calendar_ids("primary") == ("primary",)


def test_list_calendars_marks_the_primary_one():
    fake = FakeCalendar()
    fake.add_calendar("team@group.calendar.google.com", "Team")

    calendars = list_calendars(fake)

    assert {"id": "lay@gmail.com", "name": "Lay Patel", "primary": True} in calendars
    assert {"id": "team@group.calendar.google.com", "name": "Team", "primary": False} in calendars
