"""Unit tests for the Figma ``OAuthIntegration`` (no database needed).

The token endpoints are served by a local aiohttp server, so the real
request code runs: Basic-auth app credentials, form body, error handling.
Flows that need a stored credential (folder picker, sync, token refresh)
are covered end to end in ``test_figma_cognee.py``.
"""

import base64
from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qs, urlsplit

import pytest
import pytest_asyncio
from aiohttp import web
from aiohttp.test_utils import TestServer

from cognee_community_connector_figma import integration
from cognee_community_connector_figma.integration import (
    FigmaIntegration,
    configured_team_ids,
    dataset_name_for_account,
)


@pytest.fixture(autouse=True)
def settings(monkeypatch):
    for field, value in {
        "client_id": "app-id",
        "client_secret": "app-secret",
        "redirect_uri": "https://cognee.example/api/v1/integrations/figma/callback",
        "frontend_base_url": "https://cognee.example",
        "state_secret": "state-key",
        "team_ids": " 111, 222 ,",
    }.items():
        monkeypatch.setattr(integration.figma_settings, field, value)


@pytest_asyncio.fixture
async def oauth_server(monkeypatch):
    """A local stand-in for Figma's token, refresh and /v1/me endpoints."""
    seen: list[dict] = []

    async def token(request: web.Request) -> web.Response:
        form = dict(await request.post())
        seen.append({"auth": request.headers.get("Authorization"), "form": form})
        if form.get("code") == "bad":
            return web.json_response({"error": "invalid_grant"}, status=400)
        return web.json_response(
            {
                "user_id_string": "1234567890",
                "access_token": "access-1",
                "token_type": "bearer",
                "expires_in": 7776000,
                "refresh_token": "refresh-1",
            }
        )

    async def me(request: web.Request) -> web.Response:
        assert request.headers["Authorization"] == "Bearer access-1"
        return web.json_response({"id": "1234567890", "handle": "Ada", "email": "ada@acme.io"})

    app = web.Application()
    app.router.add_post("/v1/oauth/token", token)
    app.router.add_get("/v1/me", me)
    server = TestServer(app)
    await server.start_server()
    base = str(server.make_url(""))
    monkeypatch.setattr(integration, "_TOKEN_URL", f"{base}/v1/oauth/token")
    monkeypatch.setattr(integration, "_ME_URL", f"{base}/v1/me")
    yield seen
    await server.close()


def test_authorize_url_requests_read_only_scopes():
    url = urlsplit(FigmaIntegration().authorize_url("signed-state"))
    params = {key: values[0] for key, values in parse_qs(url.query).items()}

    assert f"{url.scheme}://{url.netloc}{url.path}" == "https://www.figma.com/oauth"
    assert params == {
        "client_id": "app-id",
        "redirect_uri": "https://cognee.example/api/v1/integrations/figma/callback",
        "scope": "current_user:read file_content:read file_comments:read folders:read",
        "state": "signed-state",
        "response_type": "code",
    }
    assert all(scope.endswith(":read") for scope in params["scope"].split())


def test_missing_setting_names_the_variable(monkeypatch):
    monkeypatch.setattr(integration.figma_settings, "client_id", "")
    with pytest.raises(RuntimeError, match="FIGMA_CLIENT_ID is not configured"):
        FigmaIntegration().authorize_url("state")


def test_team_ids_are_parsed_from_a_comma_separated_setting():
    assert configured_team_ids() == ["111", "222"]


@pytest.mark.asyncio
async def test_exchange_callback_sends_basic_auth_and_attaches_the_profile(oauth_server):
    token_response = await FigmaIntegration().exchange_callback("the-code", {"code": "the-code"})

    request = oauth_server[0]
    expected = base64.b64encode(b"app-id:app-secret").decode()
    assert request["auth"] == f"Basic {expected}"
    # App credentials travel in the header only, never in the form body.
    assert request["form"] == {
        "redirect_uri": "https://cognee.example/api/v1/integrations/figma/callback",
        "code": "the-code",
        "grant_type": "authorization_code",
    }
    assert token_response["me"]["email"] == "ada@acme.io"


@pytest.mark.asyncio
async def test_rejected_code_exchange_fails_without_echoing_the_body(oauth_server):
    with pytest.raises(RuntimeError, match="HTTP 400") as error:
        await FigmaIntegration().exchange_code("bad")
    assert "invalid_grant" not in str(error.value)


def test_parse_installation_keeps_secrets_out_of_metadata():
    before = datetime.now(UTC)
    installation = FigmaIntegration().parse_installation(
        {
            "user_id_string": "1234567890",
            "user_id": 1234567890,
            "access_token": "access-1",
            "refresh_token": "refresh-1",
            "expires_in": 7776000,
            "me": {"id": "1234567890", "handle": "Ada", "email": "ada@acme.io"},
        }
    )

    assert installation.provider_account_id == "1234567890"
    assert installation.token_payload == {"access_token": "access-1", "refresh_token": "refresh-1"}
    assert installation.provider_metadata == {
        "email": "ada@acme.io",
        "handle": "Ada",
        "selected_folder_ids": [],
    }
    assert installation.account_label == "ada@acme.io"
    assert installation.token_expires_at - before >= timedelta(days=89)


def test_parse_installation_requires_a_user_id():
    with pytest.raises(ValueError, match="no user id"):
        FigmaIntegration().parse_installation({"access_token": "access-1"})


def test_dataset_name_is_stable_and_disambiguated():
    first = dataset_name_for_account("ada@acme.io", "1")
    assert first == dataset_name_for_account("ada@acme.io", "1")
    assert first.startswith("figma_ada_acme_io_")
    assert first != dataset_name_for_account("ada@acme.io", "2")
