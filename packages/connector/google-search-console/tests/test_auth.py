"""Google sign-in: the three credential sources, token renewal and the token file."""

import json
import os
import stat

import httpx
import pytest
from fake_search_console import (
    ACCESS_TOKEN,
    CLIENT_ID,
    CLIENT_SECRET,
    REFRESH_TOKEN,
    FakeSearchConsole,
)

from cognee_community_connector_google_search_console import (
    GoogleSearchConsoleSettings,
    SearchConsoleAuthError,
    SearchConsoleCredentials,
)
from cognee_community_connector_google_search_console.auth import (
    credentials_from_token_file,
    resolve_credentials,
)

ENV_VARS = (
    "GOOGLE_SEARCH_CONSOLE_ACCESS_TOKEN",
    "GOOGLE_SEARCH_CONSOLE_REFRESH_TOKEN",
    "GOOGLE_SEARCH_CONSOLE_CLIENT_ID",
    "GOOGLE_SEARCH_CONSOLE_CLIENT_SECRET",
    "GOOGLE_SEARCH_CONSOLE_TOKEN_PATH",
    "GOOGLE_SEARCH_CONSOLE_CREDENTIALS_PATH",
)


@pytest.fixture(autouse=True)
def clean_environment(monkeypatch, tmp_path):
    for name in ENV_VARS:
        monkeypatch.delenv(name, raising=False)
    # Never pick up a real token file from the developer's home directory.
    monkeypatch.setenv("HOME", str(tmp_path))


@pytest.fixture
def fake(today):
    return FakeSearchConsole(today)


def _refreshable(fake):
    return SearchConsoleCredentials(
        refresh_token=REFRESH_TOKEN,
        client_id=CLIENT_ID,
        client_secret=CLIENT_SECRET,
        transport=fake.transport(),
    )


def test_an_access_token_is_used_as_given():
    assert SearchConsoleCredentials(access_token=ACCESS_TOKEN).token() == ACCESS_TOKEN


def test_a_refresh_token_is_exchanged_for_an_access_token(fake):
    credentials = _refreshable(fake)

    assert credentials.token().startswith(ACCESS_TOKEN)
    assert fake.token_requests == 1
    credentials.token()
    assert fake.token_requests == 1, "a token is renewed only when asked"


def test_credentials_need_a_token_or_a_complete_refresh_grant():
    with pytest.raises(ValueError, match="access token"):
        SearchConsoleCredentials(refresh_token="x", client_id="y")


def test_a_refused_refresh_reports_google_s_code_only(fake):
    credentials = SearchConsoleCredentials(
        refresh_token="revoked",
        client_id=CLIENT_ID,
        client_secret=CLIENT_SECRET,
        transport=fake.transport(),
    )
    with pytest.raises(SearchConsoleAuthError, match="invalid_grant") as error:
        credentials.token()
    assert "revoked" not in str(error.value)
    assert CLIENT_SECRET not in str(error.value)


def test_a_bare_access_token_cannot_be_renewed():
    with pytest.raises(SearchConsoleAuthError, match="token_expired"):
        SearchConsoleCredentials(access_token="x").refresh()


def test_a_network_failure_while_refreshing_is_reported_without_details():
    def down(_request):
        raise httpx.ConnectError("boom: secret-host")

    credentials = SearchConsoleCredentials(
        refresh_token=REFRESH_TOKEN,
        client_id=CLIENT_ID,
        client_secret=CLIENT_SECRET,
        transport=httpx.MockTransport(down),
    )
    with pytest.raises(SearchConsoleAuthError, match="ConnectError") as error:
        credentials.token()
    assert "secret-host" not in str(error.value)


def test_settings_read_the_prefixed_environment(monkeypatch):
    monkeypatch.setenv("GOOGLE_SEARCH_CONSOLE_REFRESH_TOKEN", "from-env")
    monkeypatch.setenv("GOOGLE_SEARCH_CONSOLE_CLIENT_ID", "id-from-env")

    settings = GoogleSearchConsoleSettings()

    assert (settings.refresh_token, settings.client_id) == ("from-env", "id-from-env")


def test_an_access_token_wins_over_a_refresh_token(monkeypatch):
    monkeypatch.setenv("GOOGLE_SEARCH_CONSOLE_REFRESH_TOKEN", "r")
    monkeypatch.setenv("GOOGLE_SEARCH_CONSOLE_CLIENT_ID", "c")
    monkeypatch.setenv("GOOGLE_SEARCH_CONSOLE_CLIENT_SECRET", "s")

    assert resolve_credentials(access_token="given").token() == "given"


def test_a_refresh_token_from_the_environment_can_renew(monkeypatch):
    monkeypatch.setenv("GOOGLE_SEARCH_CONSOLE_REFRESH_TOKEN", REFRESH_TOKEN)
    monkeypatch.setenv("GOOGLE_SEARCH_CONSOLE_CLIENT_ID", CLIENT_ID)
    monkeypatch.setenv("GOOGLE_SEARCH_CONSOLE_CLIENT_SECRET", CLIENT_SECRET)

    assert resolve_credentials().can_refresh


def test_the_cached_token_file_is_used(tmp_path):
    token_file = tmp_path / "token.json"
    token_file.write_text(
        json.dumps(
            {
                "token": "cached",
                "refresh_token": REFRESH_TOKEN,
                "client_id": CLIENT_ID,
                "client_secret": CLIENT_SECRET,
                "token_uri": "https://oauth2.googleapis.com/token",
            }
        )
    )

    credentials = credentials_from_token_file(str(token_file))

    assert credentials.token() == "cached"
    assert credentials.can_refresh


def test_the_token_path_setting_points_at_the_cached_file(monkeypatch, tmp_path):
    token_file = tmp_path / "elsewhere.json"
    token_file.write_text(json.dumps({"token": "elsewhere"}))
    monkeypatch.setenv("GOOGLE_SEARCH_CONSOLE_TOKEN_PATH", str(token_file))

    assert resolve_credentials().token() == "elsewhere"


def test_missing_credentials_say_how_to_sign_in():
    with pytest.raises(ValueError, match="authorize"):
        resolve_credentials()


def test_authorize_needs_the_client_secrets_file(tmp_path):
    from cognee_community_connector_google_search_console import authorize

    with pytest.raises(FileNotFoundError, match="CREDENTIALS_PATH"):
        authorize(str(tmp_path / "missing.json"), str(tmp_path / "token.json"))


def test_authorize_writes_a_private_token_file(monkeypatch, tmp_path):
    """The browser consent is stubbed; what is checked is what lands on disk."""
    import google_auth_oauthlib.flow as flow_module

    from cognee_community_connector_google_search_console import authorize

    class FakeCreds:
        def to_json(self):
            return json.dumps({"token": "t", "refresh_token": REFRESH_TOKEN})

    class FakeFlow:
        scopes = None

        @classmethod
        def from_client_secrets_file(cls, path, scopes):
            cls.scopes = scopes
            return cls()

        def run_local_server(self, port):
            return FakeCreds()

    monkeypatch.setattr(flow_module, "InstalledAppFlow", FakeFlow)
    secrets, token_file = tmp_path / "client.json", tmp_path / "token.json"
    secrets.write_text("{}")
    token_file.write_text("old")
    os.chmod(token_file, 0o644)

    credentials = authorize(str(secrets), str(token_file))

    assert credentials.token() == "t"
    assert FakeFlow.scopes == ["https://www.googleapis.com/auth/webmasters.readonly"]
    assert stat.S_IMODE(os.stat(token_file).st_mode) == 0o600


@pytest.mark.parametrize("token", ["abc\n", " abc", "ab c", "abc\t"])
def test_a_token_with_whitespace_is_refused_without_echoing_it(token):
    with pytest.raises(ValueError, match="whitespace") as error:
        SearchConsoleCredentials(access_token=token)
    assert "abc" not in str(error.value)
