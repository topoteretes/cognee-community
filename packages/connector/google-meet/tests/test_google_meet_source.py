"""google_meet_source() factory, settings and the Google sign-in builders."""

import json
import os
import stat

import pytest
from cognee.modules.integrations.google import GoogleAuthError, GoogleSettings
from cognee.tasks.ingestion.dlt_utils import DOCUMENT_SOURCE_ATTR, PIPELINE_SCOPE_ATTR
from fake_meet import FakeDrive, FakeMeet

import cognee_community_connector_google_meet.google_meet as module
from cognee_community_connector_google_meet import (
    GoogleMeetSettings,
    MeetServices,
    build_meet_services,
    google_meet_source,
)

MEET_SCOPE = "https://www.googleapis.com/auth/meetings.space.readonly"
DRIVE_SCOPE = "https://www.googleapis.com/auth/drive.meet.readonly"
ENV = (
    "GOOGLE_MEET_CLIENT_ID",
    "GOOGLE_MEET_CLIENT_SECRET",
    "GOOGLE_MEET_REFRESH_TOKEN",
    "GOOGLE_MEET_CREDENTIALS_PATH",
    "GOOGLE_MEET_TOKEN_PATH",
    "GOOGLE_MEET_ACCESS_TOKEN",
)


@pytest.fixture(autouse=True)
def no_meet_env(monkeypatch):
    for name in ENV:
        monkeypatch.delenv(name, raising=False)


# ---------------------------------------------------------------------------
# Settings inherited from cognee
# ---------------------------------------------------------------------------
def test_settings_inherit_cognee_google_settings_and_read_their_prefix(monkeypatch):
    monkeypatch.setenv("GOOGLE_MEET_CLIENT_ID", "client-123")
    monkeypatch.setenv("GOOGLE_MEET_REFRESH_TOKEN", "refresh-abc")

    settings = GoogleMeetSettings()

    assert isinstance(settings, GoogleSettings)
    assert settings.client_id == "client-123"
    assert settings.refresh_token == "refresh-abc"


def test_refresh_token_without_client_credentials_raises_cognee_auth_error():
    settings = GoogleMeetSettings(refresh_token="refresh-abc")

    with pytest.raises(GoogleAuthError, match="missing_client_credentials"):
        module._build_services_from_refresh_token(settings, True)


# ---------------------------------------------------------------------------
# Installed-app flow
# ---------------------------------------------------------------------------
class _FakeCreds:
    valid = True
    expired = False
    refresh_token = "r"

    def __init__(self, scopes=()):
        self.scopes = list(scopes)

    def to_json(self):
        return json.dumps({"token": "t", "scopes": self.scopes})


def _fake_libraries(calls):
    class Flow:
        def __init__(self, scopes):
            self.scopes = scopes

        @classmethod
        def from_client_secrets_file(cls, path, scopes):
            calls["scopes"] = scopes
            return cls(scopes)

        def run_local_server(self, port):
            calls["browser"] = True
            return _FakeCreds(self.scopes)

    class Credentials:
        def __init__(self, **kwargs):
            calls["credentials"] = kwargs

        @staticmethod
        def from_authorized_user_file(path, scopes):
            return _FakeCreds(scopes)

    def build(name, version, credentials, cache_discovery):
        calls.setdefault("built", []).append((name, version))
        return f"{name}-service"

    return lambda: (object, Credentials, Flow, build)


def test_first_login_writes_a_private_token_file_with_only_the_readonly_scopes(
    tmp_path, monkeypatch
):
    calls: dict = {}
    monkeypatch.setattr(module, "_google_libraries", _fake_libraries(calls))
    secrets = tmp_path / "client_secret.json"
    secrets.write_text("{}")
    token = tmp_path / "token.json"

    services = build_meet_services(str(secrets), str(token), login=True)

    assert services == MeetServices(meet="meet-service", drive="drive-service")
    assert calls["built"] == [("meet", "v2"), ("drive", "v3")]
    assert calls["scopes"] == [MEET_SCOPE, DRIVE_SCOPE]
    assert calls["browser"] is True
    assert stat.S_IMODE(os.stat(token).st_mode) == 0o600


def test_without_drive_checks_only_the_meet_scope_is_asked(tmp_path, monkeypatch):
    calls: dict = {}
    monkeypatch.setattr(module, "_google_libraries", _fake_libraries(calls))
    secrets = tmp_path / "client_secret.json"
    secrets.write_text("{}")

    services = build_meet_services(str(secrets), str(tmp_path / "t.json"), False, login=True)

    assert calls["scopes"] == [MEET_SCOPE]
    assert services == MeetServices(meet="meet-service", drive=None)
    assert calls["built"] == [("meet", "v2")]


def test_cached_token_skips_the_browser(tmp_path, monkeypatch):
    calls: dict = {}
    monkeypatch.setattr(module, "_google_libraries", _fake_libraries(calls))
    token = tmp_path / "token.json"
    token.write_text(json.dumps({"scopes": [MEET_SCOPE, DRIVE_SCOPE]}))

    build_meet_services(str(tmp_path / "missing.json"), str(token))

    assert "browser" not in calls


def test_cached_token_missing_the_drive_scope_asks_for_consent_again(tmp_path, monkeypatch):
    calls: dict = {}
    monkeypatch.setattr(module, "_google_libraries", _fake_libraries(calls))
    secrets = tmp_path / "client_secret.json"
    secrets.write_text("{}")
    token = tmp_path / "token.json"
    token.write_text(json.dumps({"scopes": [MEET_SCOPE]}))

    build_meet_services(str(secrets), str(token), login=True)

    assert calls["browser"] is True
    assert json.loads(token.read_text())["scopes"] == [MEET_SCOPE, DRIVE_SCOPE]


@pytest.mark.parametrize("cached_scopes", [None, [MEET_SCOPE]])
def test_a_sync_never_opens_a_browser(tmp_path, monkeypatch, cached_scopes):
    # No token, or one missing the Drive scope: only examples/login.py may ask.
    calls: dict = {}
    monkeypatch.setattr(module, "_google_libraries", _fake_libraries(calls))
    secrets = tmp_path / "client_secret.json"
    secrets.write_text("{}")
    token = tmp_path / "token.json"
    if cached_scopes:
        token.write_text(json.dumps({"scopes": cached_scopes}))

    with pytest.raises(GoogleAuthError, match="login_required"):
        build_meet_services(str(secrets), str(token))

    assert "browser" not in calls


def test_missing_client_secrets_file_is_a_clear_error(tmp_path, monkeypatch):
    monkeypatch.setattr(module, "_google_libraries", _fake_libraries({}))

    with pytest.raises(FileNotFoundError, match="GOOGLE_MEET_CREDENTIALS_PATH"):
        build_meet_services(str(tmp_path / "nope.json"), str(tmp_path / "token.json"), login=True)


@pytest.mark.parametrize(
    ("check_drive", "scopes"), [(True, [MEET_SCOPE, DRIVE_SCOPE]), (False, [MEET_SCOPE])]
)
def test_headless_refresh_token_asks_for_the_same_scopes(monkeypatch, check_drive, scopes):
    calls: dict = {}
    monkeypatch.setattr(module, "_google_libraries", _fake_libraries(calls))
    settings = GoogleMeetSettings(client_id="id", client_secret="secret", refresh_token="r")

    module._build_services_from_refresh_token(settings, check_drive)

    assert calls["credentials"]["scopes"] == scopes
    assert calls["credentials"]["refresh_token"] == "r"


# ---------------------------------------------------------------------------
# google_meet_source()
# ---------------------------------------------------------------------------
def test_resource_opts_into_document_mode_and_pipeline_scope():
    services = MeetServices(meet=FakeMeet(), drive=FakeDrive())
    resource = google_meet_source(services=services, resource_name="work_meet")

    assert resource.name == "work_meet"
    assert getattr(resource, DOCUMENT_SOURCE_ATTR) == "google_meet"
    assert getattr(resource, PIPELINE_SCOPE_ATTR) == "work_meet"
    assert resource.cognee_sync_stats == {}


@pytest.mark.parametrize("name", ["", "x" * 65, "Work-Meet", "work meet", "1meet"])
def test_resource_name_that_would_lose_or_share_state_is_refused(name):
    services = MeetServices(meet=FakeMeet(), drive=FakeDrive())

    with pytest.raises(ValueError, match="resource_name"):
        google_meet_source(services=services, resource_name=name)


def test_missing_credentials_fail_when_the_source_is_built():
    with pytest.raises(ValueError, match="credentials are missing"):
        google_meet_source()


@pytest.mark.parametrize(
    "env",
    [
        {"GOOGLE_MEET_ACCESS_TOKEN": "t"},
        {"GOOGLE_MEET_REFRESH_TOKEN": "r"},
        {"GOOGLE_MEET_CREDENTIALS_PATH": "/tmp/client.json"},
    ],
)
def test_any_one_auth_method_from_the_environment_is_enough(monkeypatch, env):
    for name, value in env.items():
        monkeypatch.setenv(name, value)

    google_meet_source()  # builds without contacting Google


def test_drive_checks_without_a_drive_client_are_refused():
    with pytest.raises(ValueError, match="needs a Drive client"):
        google_meet_source(services=MeetServices(meet=FakeMeet()))

    google_meet_source(services=MeetServices(meet=FakeMeet()), check_drive_deletes=False)
    google_meet_source(services=MeetServices(meet=FakeMeet()), include_transcripts=False)


def test_access_token_scopes_follow_the_drive_setting(monkeypatch):
    calls: dict = {}
    monkeypatch.setattr(module, "_google_libraries", _fake_libraries(calls))

    services = module.build_meet_services_from_access_token("t", check_drive_deletes=False)

    assert calls["credentials"]["scopes"] == [MEET_SCOPE]
    assert services.drive is None


@pytest.mark.parametrize(
    ("option", "value"),
    [("include_transcripts", "false"), ("check_drive_deletes", 0), ("check_active", "yes")],
)
def test_options_of_the_wrong_type_are_refused(option, value):
    services = MeetServices(meet=FakeMeet(), drive=FakeDrive())

    with pytest.raises(TypeError, match=option):
        google_meet_source(services=services, **{option: value})


class _ClosingClient:
    def __init__(self, client):
        self.client = client
        self.closed = 0

    def __getattr__(self, name):
        return getattr(self.client, name)

    def close(self):
        self.closed += 1


def _run(resource, pipelines_dir) -> None:
    import dlt

    dlt.pipeline(pipeline_name="meet_close_test", pipelines_dir=str(pipelines_dir)).extract(
        resource
    )


def test_clients_built_for_a_run_are_closed_after_it(monkeypatch, tmp_path):
    meet, drive = _ClosingClient(FakeMeet()), _ClosingClient(FakeDrive())
    monkeypatch.setattr(
        module,
        "build_meet_services_from_access_token",
        lambda token, uses_drive: MeetServices(meet=meet, drive=drive),
    )

    _run(google_meet_source(access_token="t"), tmp_path)

    assert (meet.closed, drive.closed) == (1, 1)


def test_clients_passed_in_are_left_open_for_their_owner(tmp_path):
    meet, drive = _ClosingClient(FakeMeet()), _ClosingClient(FakeDrive())

    _run(google_meet_source(services=MeetServices(meet=meet, drive=drive)), tmp_path)

    assert (meet.closed, drive.closed) == (0, 0)
