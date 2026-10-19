"""outlook_source() factory and the shared Microsoft Graph sign-in."""

import os
import stat

import pytest
from cognee.tasks.ingestion.dlt_utils import DOCUMENT_SOURCE_ATTR, PIPELINE_SCOPE_ATTR
from fake_graph import BASE

from cognee_community_connector_outlook import GraphTokenProvider, outlook_source
from cognee_community_connector_outlook.graph_auth import GraphAuth
from cognee_community_connector_outlook.outlook import (
    _normalize_mail_folders,
    build_graph_client,
)


@pytest.fixture(autouse=True)
def no_outlook_env(monkeypatch):
    for name in (
        "OUTLOOK_CLIENT_ID",
        "OUTLOOK_AUTHORITY",
        "OUTLOOK_TOKEN_CACHE_PATH",
        "OUTLOOK_ACCESS_TOKEN",
        "OUTLOOK_MAIL_FOLDERS",
        "OUTLOOK_MAIL_RECEIVED_AFTER",
    ):
        monkeypatch.delenv(name, raising=False)


class FakeMsalApp:
    """Stands in for msal.PublicClientApplication."""

    def __init__(self, cached=None, device_result=None, flow=None):
        self.cached = cached
        self.device_result = device_result or {"access_token": "from-device-login"}
        self.flow = flow or {"user_code": "ABC123", "message": "Go to the login page"}
        self.device_logins = 0
        self.scopes = None

    def get_accounts(self):
        return [{"username": "lay@outlook.com"}] if self.cached else []

    def acquire_token_silent(self, scopes, account):
        self.scopes = scopes
        return self.cached

    def initiate_device_flow(self, scopes):
        self.scopes = scopes
        return self.flow

    def acquire_token_by_device_flow(self, flow):
        self.device_logins += 1
        return self.device_result


# ---------------------------------------------------------------------------
# Sign-in
# ---------------------------------------------------------------------------
def test_cached_token_is_used_without_a_device_login():
    app = FakeMsalApp(cached={"access_token": "cached"})
    provider = GraphTokenProvider(msal_app=app, scopes=["Mail.Read"])

    assert provider.token() == "cached"
    assert app.device_logins == 0
    assert app.scopes == ["Mail.Read"]


def test_device_login_runs_when_nothing_is_cached(capsys):
    app = FakeMsalApp()
    provider = GraphTokenProvider(msal_app=app)

    assert provider.token() == "from-device-login"
    assert app.device_logins == 1
    assert "Go to the login page" in capsys.readouterr().out


def test_failed_sign_in_raises():
    app = FakeMsalApp(device_result={"error": "authorization_declined"})

    with pytest.raises(RuntimeError, match="authorization_declined"):
        GraphTokenProvider(msal_app=app).token()


def test_device_login_that_cannot_start_raises():
    app = FakeMsalApp(flow={"error_description": "invalid client"})

    with pytest.raises(RuntimeError, match="invalid client"):
        GraphTokenProvider(msal_app=app).token()


def test_ready_made_access_token_is_used_as_is():
    provider = GraphTokenProvider(access_token="explorer-token")

    assert provider.token() == "explorer-token"


def test_auth_header_is_set_on_every_request():
    import requests

    request = GraphAuth(GraphTokenProvider(access_token="abc"))(requests.Request("GET", "x"))

    assert request.headers["Authorization"] == "Bearer abc"


def test_token_cache_is_written_private(tmp_path):
    pytest.importorskip("msal")
    cache_path = tmp_path / "token_cache.json"
    provider = GraphTokenProvider(client_id="00000000-0000-0000-0000-000000000000")
    provider._token_cache_path = str(cache_path)
    provider._app = FakeMsalApp(cached={"access_token": "cached"})
    provider._cache.has_state_changed = True

    provider.token()

    assert stat.S_IMODE(os.stat(cache_path).st_mode) == 0o600


# ---------------------------------------------------------------------------
# outlook_source()
# ---------------------------------------------------------------------------
def test_resource_opts_into_document_mode_and_pipeline_scope():
    resource = outlook_source(access_token="t", resource_name="work_outlook")

    assert resource.name == "work_outlook"
    assert getattr(resource, DOCUMENT_SOURCE_ATTR) == "outlook"
    assert getattr(resource, PIPELINE_SCOPE_ATTR) == "work_outlook"
    assert resource.cognee_sync_stats == {}


def test_missing_credentials_fail_when_the_source_is_built():
    with pytest.raises(ValueError, match="credentials are missing"):
        outlook_source()


def test_empty_folder_list_is_refused_instead_of_forgetting_mail():
    with pytest.raises(ValueError, match="mail_folders is empty"):
        outlook_source([], access_token="t")


def test_turning_everything_off_is_refused():
    with pytest.raises(ValueError, match="Nothing to sync"):
        outlook_source(access_token="t", include_mail=False, include_calendar=False)


def test_scopes_follow_what_is_synced():
    def scopes(**kwargs):
        captured = {}

        class Recorder(GraphTokenProvider):
            def __init__(self, **kw):
                captured.update(kw)
                super().__init__(**kw)

        import cognee_community_connector_outlook.outlook as module

        original = module.GraphTokenProvider
        module.GraphTokenProvider = Recorder
        try:
            outlook_source(client_id="app", **kwargs)
        finally:
            module.GraphTokenProvider = original
        return captured["scopes"]

    assert scopes() == ["User.Read", "Mail.Read", "Calendars.Read"]
    assert scopes(include_mail_bodies=False) == ["User.Read", "Mail.ReadBasic", "Calendars.Read"]
    assert scopes(include_calendar=False) == ["User.Read", "Mail.Read"]
    assert scopes(include_mail=False) == ["User.Read", "Calendars.Read"]


def test_folders_are_normalized():
    folders = [" Inbox ", "archive", "AAMkCustomId", "inbox"]

    assert _normalize_mail_folders(folders) == ("inbox", "archive", "AAMkCustomId")


def test_environment_is_left_to_the_example(monkeypatch):
    monkeypatch.setenv("OUTLOOK_ACCESS_TOKEN", "token")
    monkeypatch.setenv("OUTLOOK_MAIL_FOLDERS", "archive")

    assert _normalize_mail_folders(None) == ("inbox", "sentitems")
    with pytest.raises(ValueError, match="credentials are missing"):
        outlook_source()


@pytest.mark.parametrize("name", ["", "x" * 65, "Work-Outlook", "work outlook", "1outlook"])
def test_resource_name_that_would_lose_or_share_state_is_refused(name):
    with pytest.raises(ValueError, match="resource_name"):
        outlook_source(resource_name=name, access_token="token")


def test_service_is_used_as_the_graph_client():
    service = build_graph_client(None, BASE)

    resource = outlook_source(service=service)

    assert resource.name == "outlook_items"


def test_default_folders_are_inbox_and_sent_items():
    assert _normalize_mail_folders(None) == ("inbox", "sentitems")
    assert _normalize_mail_folders("SentItems") == ("sentitems",)
