"""outlook_source() factory and the shared Microsoft Graph sign-in."""

import os
import socket
import stat

import pytest
import requests
import responses
from cognee.tasks.ingestion.dlt_utils import DOCUMENT_SOURCE_ATTR, PIPELINE_SCOPE_ATTR
from fake_graph import BASE, FakeGraph

import cognee_community_connector_outlook.outlook as outlook_module
from cognee_community_connector_outlook import GraphTokenProvider, outlook_source
from cognee_community_connector_outlook.graph_auth import (
    GraphAuth,
    GraphHostError,
    GraphLoginRequiredError,
)
from cognee_community_connector_outlook.outlook import (
    _normalize_mail_folders,
    _normalize_received_after,
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

    def __init__(self, cached=None, device_result=None, flow=None, accounts=None):
        self.cached = cached
        self.device_result = device_result or {"access_token": "from-device-login"}
        self.flow = flow or {"user_code": "ABC123", "message": "Go to the login page"}
        self.device_logins = 0
        self.scopes = None
        self.account = None
        self.accounts = accounts or (["lay@outlook.com"] if cached else [])

    def get_accounts(self, username=None):
        names = [a for a in self.accounts if username is None or a.lower() == username.lower()]
        return [{"username": name} for name in names]

    def acquire_token_silent(self, scopes, account):
        self.scopes = scopes
        self.account = account
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


@pytest.mark.parametrize("cached", [None, {"error": "invalid_grant"}])
def test_a_sync_never_starts_a_device_login(capsys, cached):
    # Nothing cached, or a refresh that failed: a person has to log in again.
    app = FakeMsalApp(cached=cached, accounts=["lay@outlook.com"])

    with pytest.raises(GraphLoginRequiredError, match=r"examples/login\.py"):
        GraphTokenProvider(msal_app=app).token()

    assert app.device_logins == 0
    assert capsys.readouterr().out == ""


def test_login_runs_the_device_flow(capsys):
    app = FakeMsalApp()
    provider = GraphTokenProvider(msal_app=app)

    assert provider.login() == "from-device-login"
    assert app.device_logins == 1
    assert "Go to the login page" in capsys.readouterr().out


def test_failed_sign_in_raises():
    app = FakeMsalApp(device_result={"error": "authorization_declined"})

    with pytest.raises(RuntimeError, match="authorization_declined"):
        GraphTokenProvider(msal_app=app).login()


def test_device_login_that_cannot_start_raises():
    app = FakeMsalApp(flow={"error_description": "invalid client"})

    with pytest.raises(RuntimeError, match="invalid client"):
        GraphTokenProvider(msal_app=app).login()


def test_a_cache_with_several_accounts_needs_the_account_named():
    app = FakeMsalApp(cached={"access_token": "t"}, accounts=["lay@outlook.com", "work@corp.com"])

    with pytest.raises(ValueError, match="account="):
        GraphTokenProvider(msal_app=app).token()

    GraphTokenProvider(msal_app=app, account="Work@corp.com").token()
    assert app.account == {"username": "work@corp.com"}


def test_ready_made_access_token_is_used_as_is():
    provider = GraphTokenProvider(access_token="explorer-token")

    assert provider.token() == "explorer-token"


def test_auth_header_is_set_on_every_request():
    auth = GraphAuth(GraphTokenProvider(access_token="abc"), "https://graph.microsoft.com/v1.0")

    request = auth(requests.Request("GET", "https://GRAPH.microsoft.com:443/v1.0/me/events"))

    assert request.headers["Authorization"] == "Bearer abc"


@pytest.mark.parametrize(
    "url",
    [
        "https://graph.microsoft.com.evil.test/v1.0/me",
        "http://graph.microsoft.com/v1.0/me",
        "https://graph.microsoft.com:8443/v1.0/me",
        "https://login.microsoftonline.com/common",
    ],
)
def test_the_token_is_never_sent_to_another_host(url):
    auth = GraphAuth(GraphTokenProvider(access_token="abc"), "https://graph.microsoft.com/v1.0")
    request = requests.Request("GET", url)

    with pytest.raises(GraphHostError):
        auth(request)

    assert "Authorization" not in request.headers


def test_a_next_page_link_to_another_host_fails_the_folder_without_the_token():
    graph = FakeGraph()
    for i in range(3):
        graph.put_message("inbox", f"Message {i}")  # page_size 2, so a next link exists
    original = graph._page

    def link_elsewhere(items, final_link):
        status, headers, body = original(items, final_link)
        return status, headers, body.replace("https://graph.test", "https://evil.test")

    graph._page = link_elsewhere
    stats: dict = {}
    with responses.RequestsMock(assert_all_requests_are_fired=False) as mock:
        graph.install(mock)
        client = build_graph_client(GraphAuth(GraphTokenProvider(access_token="t"), BASE), BASE)
        config = outlook_module._OutlookConfig(
            include_mail=True,
            mail_folders=("inbox",),
            include_mail_bodies=True,
            mail_received_after=None,
            include_calendar=False,
            calendar_days_back=30,
            calendar_days_ahead=30,
            calendar_window_refresh_days=7,
        )
        with pytest.raises(outlook_module.OutlookError, match="another host"):
            list(outlook_module._iter_rows(client, config, {}, stats))

    assert all(r.url.startswith(BASE) for r in graph.requests)
    assert stats["failed_mail"] == 1 and stats["failed_connection"] == 0


def _provider_with_cache(cache_path):
    import msal

    provider = GraphTokenProvider(
        client_id="00000000-0000-0000-0000-000000000000", token_cache_path=str(cache_path)
    )
    provider._app = FakeMsalApp(cached={"access_token": "cached"})
    provider._cache = msal.SerializableTokenCache()
    provider._cache.has_state_changed = True
    return provider


def test_token_cache_is_written_private(tmp_path):
    cache_path = tmp_path / "token_cache.json"

    _provider_with_cache(cache_path).token()

    assert stat.S_IMODE(os.stat(cache_path).st_mode) == 0o600


def test_an_existing_readable_token_cache_is_made_private(tmp_path):
    # Copied over or restored from a backup with the usual 0644.
    cache_path = tmp_path / "token_cache.json"
    cache_path.write_text("{}")
    os.chmod(cache_path, 0o644)

    _provider_with_cache(cache_path).token()

    assert stat.S_IMODE(os.stat(cache_path).st_mode) == 0o600


@pytest.fixture
def no_network(monkeypatch):
    def refuse(*_args, **_kwargs):
        raise OSError("network blocked in this test")

    monkeypatch.setattr(socket, "getaddrinfo", refuse)
    monkeypatch.setattr(socket.socket, "connect", refuse)


def test_building_the_source_with_a_client_id_needs_no_network(no_network, tmp_path):
    resource = outlook_source(
        client_id="00000000-0000-0000-0000-000000000000",
        token_cache_path=str(tmp_path / "cache.json"),
    )

    assert resource.name == "outlook_items"


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
    folders = [" Inbox ", "archive", "AAMkCustomId", "inbox", "Outbox", "outbox", "", "  "]

    assert _normalize_mail_folders(folders) == ("inbox", "archive", "AAMkCustomId", "outbox")


@pytest.mark.parametrize("folders", [[""], [" "], ["", " "]])
def test_blank_folders_are_dropped_and_an_empty_list_refused(folders):
    with pytest.raises(ValueError, match="mail_folders is empty"):
        outlook_source(folders, access_token="t")


@pytest.mark.parametrize("folder", ["DeletedItems", "deleteditems", "JunkEmail"])
def test_deleted_items_and_junk_are_refused(folder):
    with pytest.raises(ValueError, match="Deleted Items and Junk Email"):
        outlook_source(["inbox", folder], access_token="t")


@pytest.mark.parametrize(
    "kwargs, match",
    [
        ({"calendar_window_refresh_days": 0}, "calendar_window_refresh_days"),
        ({"calendar_window_refresh_days": 1.5}, "calendar_window_refresh_days"),
        ({"calendar_days_back": -40}, "calendar_days_back"),
        ({"calendar_days_ahead": -1}, "calendar_days_back"),
        ({"calendar_days_back": 0, "calendar_days_ahead": 0}, "not both 0"),
        ({"calendar_days_back": True}, "calendar_days_back"),
        ({"time_zone": "Mars/Olympus"}, "time_zone"),
        ({"mail_received_after": "last tuesday"}, "mail_received_after"),
    ],
)
def test_settings_that_cannot_work_are_refused(kwargs, match):
    with pytest.raises(ValueError, match=match):
        outlook_source(access_token="t", **kwargs)


@pytest.mark.parametrize(
    "value",
    ["2026-01-01T05:30:00+05:30", "2026-01-01T00:00:00Z", "2026-01-01", "2026-01-01T00:00:00"],
)
def test_received_after_is_sent_to_graph_in_utc(value):
    assert _normalize_received_after(value) == "2026-01-01T00:00:00Z"


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


class _ClosingFakeGraph:
    """Records every Graph client the resource builds and whether it was closed."""

    def __init__(self, monkeypatch):
        self.built = []
        self.closed = []
        real = outlook_module.build_graph_client

        def build(auth, base_url):
            client = real(auth, base_url)
            self.built.append(client)
            original_close = client.session.close

            def close():
                self.closed.append(client)
                original_close()

            client.session.close = close
            return client

        monkeypatch.setattr(outlook_module, "build_graph_client", build)


def test_each_run_builds_its_own_client_and_closes_it(monkeypatch, tmp_path):
    import dlt

    clients = _ClosingFakeGraph(monkeypatch)
    graph = FakeGraph()
    graph.put_message("inbox", "Hello")
    monkeypatch.setenv("DLT_DATA_DIR", str(tmp_path))
    resource = outlook_source(["inbox"], include_calendar=False, access_token="t", base_url=BASE)
    pipeline = dlt.pipeline("outlook_close_test", dev_mode=True)

    with responses.RequestsMock(assert_all_requests_are_fired=False) as mock:
        graph.install(mock)
        pipeline.extract(resource)
        pipeline.extract(resource)

    assert len(clients.built) == 2, "a client per run, so each run has its own wait budget"
    assert clients.closed == clients.built


def test_an_injected_client_is_never_closed(monkeypatch, tmp_path):
    import dlt

    graph = FakeGraph()
    graph.put_message("inbox", "Hello")
    monkeypatch.setenv("DLT_DATA_DIR", str(tmp_path))
    service = build_graph_client(GraphAuth(GraphTokenProvider(access_token="t"), BASE), BASE)
    closed = []
    service.session.close = lambda: closed.append(True)

    with responses.RequestsMock(assert_all_requests_are_fired=False) as mock:
        graph.install(mock)
        dlt.pipeline("outlook_injected_test", dev_mode=True).extract(
            outlook_source(["inbox"], include_calendar=False, service=service)
        )

    assert closed == []


def test_default_folders_are_inbox_and_sent_items():
    assert _normalize_mail_folders(None) == ("inbox", "sentitems")
    assert _normalize_mail_folders("SentItems") == ("sentitems",)
