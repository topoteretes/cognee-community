"""Unit tests for the Stripe Connect ``OAuthIntegration`` (no database needed).

Stripe's OAuth and account endpoints are served by a local aiohttp server, so
the real request code runs: Basic auth with the platform key, form body and
error handling. Flows that need a stored credential (sync of the stored
selection) are covered end to end in ``test_stripe_cognee.py``.
"""

import base64
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import pytest
import pytest_asyncio
from aiohttp import web
from aiohttp.test_utils import TestServer

from cognee_community_connector_stripe import integration
from cognee_community_connector_stripe.integration import (
    StripeIntegration,
    dataset_name_for_account,
    selected_objects,
)
from cognee_community_connector_stripe.stripe import OBJECT_TYPES

PLATFORM_KEY = "sk_test_platform"
ACCOUNT_ID = "acct_connected"


@pytest.fixture(autouse=True)
def settings(monkeypatch):
    for field, value in {
        "client_id": "ca_platform",
        "client_secret": PLATFORM_KEY,
        "redirect_uri": "https://cognee.example/api/v1/integrations/stripe/callback",
        "frontend_base_url": "https://cognee.example",
        "state_secret": "state-key",
    }.items():
        monkeypatch.setattr(integration.stripe_settings, field, value)


@pytest_asyncio.fixture
async def stripe_server(monkeypatch):
    """A local stand-in for Stripe's OAuth token, deauthorize and accounts endpoints."""
    seen: list[dict] = []

    async def token(request: web.Request) -> web.Response:
        form = dict(await request.post())
        seen.append({"path": "token", "auth": request.headers.get("Authorization"), "form": form})
        if form.get("code") == "bad":
            return web.json_response({"error": "invalid_grant"}, status=400)
        return web.json_response(
            {
                "access_token": "sk_test_deprecated",
                "refresh_token": "rt_deprecated",
                "token_type": "bearer",
                "stripe_publishable_key": "pk_test_deprecated",
                "stripe_user_id": ACCOUNT_ID,
                "scope": "read_write" if form.get("code") == "broad" else "read_only",
                "livemode": False,
            }
        )

    async def deauthorize(request: web.Request) -> web.Response:
        form = dict(await request.post())
        seen.append({"path": "deauthorize", "auth": request.headers.get("Authorization"), **form})
        return web.json_response({"stripe_user_id": form["stripe_user_id"]})

    async def account(request: web.Request) -> web.Response:
        seen.append({"path": "account", "auth": request.headers.get("Authorization")})
        return web.json_response(
            {
                "id": request.match_info["account_id"],
                "object": "account",
                "settings": {"dashboard": {"display_name": "Alphacorp"}},
            }
        )

    app = web.Application()
    app.router.add_post("/oauth/token", token)
    app.router.add_post("/oauth/deauthorize", deauthorize)
    app.router.add_get("/v1/accounts/{account_id}", account)
    server = TestServer(app)
    await server.start_server()
    base = str(server.make_url(""))
    monkeypatch.setattr(integration, "_TOKEN_URL", f"{base}/oauth/token")
    monkeypatch.setattr(integration, "_DEAUTHORIZE_URL", f"{base}/oauth/deauthorize")
    monkeypatch.setattr(integration, "_ACCOUNT_URL", f"{base}/v1/accounts/{{account_id}}")
    yield seen
    await server.close()


def _basic(key: str = PLATFORM_KEY) -> str:
    return f"Basic {base64.b64encode(f'{key}:'.encode()).decode()}"


def test_authorize_url_requests_read_only_access():
    url = urlsplit(StripeIntegration().authorize_url("signed-state"))
    params = {key: values[0] for key, values in parse_qs(url.query).items()}

    assert f"{url.scheme}://{url.netloc}{url.path}" == "https://connect.stripe.com/oauth/authorize"
    assert params == {
        "response_type": "code",
        "client_id": "ca_platform",
        "scope": "read_only",
        "redirect_uri": "https://cognee.example/api/v1/integrations/stripe/callback",
        "state": "signed-state",
    }


def test_missing_setting_names_the_variable(monkeypatch):
    monkeypatch.setattr(integration.stripe_settings, "client_id", "")
    with pytest.raises(RuntimeError, match="STRIPE_CLIENT_ID is not configured"):
        StripeIntegration().authorize_url("state")


@pytest.mark.asyncio
async def test_exchange_callback_uses_the_platform_key_and_names_the_account(stripe_server):
    token_response = await StripeIntegration().exchange_callback("the-code", {"code": "the-code"})

    token_request, account_request = stripe_server
    assert token_request["auth"] == _basic()
    # The secret key travels in the header only, never in the form body.
    assert token_request["form"] == {"grant_type": "authorization_code", "code": "the-code"}
    assert account_request["auth"] == _basic()
    assert token_response["account"]["settings"]["dashboard"]["display_name"] == "Alphacorp"


@pytest.mark.asyncio
async def test_a_broader_scope_than_read_only_is_refused(stripe_server):
    with pytest.raises(RuntimeError, match="registered as extensions") as error:
        await StripeIntegration().exchange_code("broad")

    # The message names what was granted and what the platform needs to be.
    assert "'read_write' instead of 'read_only'" in str(error.value)
    assert "must be an extension" in str(error.value)


@pytest.mark.asyncio
async def test_rejected_code_exchange_fails_without_echoing_the_body(stripe_server):
    with pytest.raises(RuntimeError, match="HTTP 400") as error:
        await StripeIntegration().exchange_code("bad")
    assert "invalid_grant" not in str(error.value)


def test_parse_installation_stores_no_deprecated_tokens():
    installation = StripeIntegration().parse_installation(
        {
            "access_token": "sk_test_deprecated",
            "refresh_token": "rt_deprecated",
            "stripe_publishable_key": "pk_test_deprecated",
            "stripe_user_id": ACCOUNT_ID,
            "scope": "read_only",
            "livemode": False,
            "account": {"business_profile": {"name": "Alphacorp LLC"}},
        }
    )

    assert installation.provider_account_id == ACCOUNT_ID
    assert installation.token_payload == {"stripe_user_id": ACCOUNT_ID}
    assert installation.provider_metadata == {
        "display_name": "Alphacorp LLC",
        "livemode": False,
        "selected_objects": [],
    }
    assert "deprecated" not in repr(installation)
    assert installation.account_label == "Alphacorp LLC"
    assert installation.scopes == "read_only"
    assert installation.token_expires_at is None


def test_parse_installation_requires_an_account_id():
    with pytest.raises(ValueError, match="no stripe_user_id"):
        StripeIntegration().parse_installation({"scope": "read_only"})


@pytest.mark.asyncio
async def test_resource_picker_offers_the_object_types():
    resources = await StripeIntegration().list_resources(credential=None)

    assert [resource["id"] for resource in resources] == list(OBJECT_TYPES)
    assert all(resource["name"] and resource["description"] for resource in resources)


@pytest.mark.parametrize(
    ("stored", "expected"),
    [(None, list(OBJECT_TYPES)), ([], []), (["customers", "invoices"], ["customers", "invoices"])],
)
def test_selection_is_three_state(stored, expected):
    credential = SimpleNamespace(provider_metadata={"selected_objects": stored})
    assert selected_objects(credential) == expected


def test_selection_refuses_unknown_types():
    credential = SimpleNamespace(provider_metadata={"selected_objects": ["cards"]})
    with pytest.raises(ValueError, match="selection must be a list"):
        selected_objects(credential)


@pytest.mark.asyncio
async def test_disconnect_deauthorizes_the_account(stripe_server):
    credential = SimpleNamespace(provider_account_id=ACCOUNT_ID)

    await StripeIntegration().revoke_remote(credential)

    [request] = stripe_server
    assert request == {
        "path": "deauthorize",
        "auth": _basic(),
        "client_id": "ca_platform",
        "stripe_user_id": ACCOUNT_ID,
    }


def test_dataset_name_comes_from_the_account_id_alone():
    """A renamed account keeps its dataset, so its data is neither orphaned nor duplicated."""
    before = SimpleNamespace(provider_account_id="acct_1", provider_metadata={"display_name": "A"})
    renamed = SimpleNamespace(provider_account_id="acct_1", provider_metadata={"display_name": "B"})
    other = SimpleNamespace(provider_account_id="acct_2", provider_metadata={"display_name": "A"})

    assert StripeIntegration().dataset_name(before) == "stripe_acct_1"
    assert StripeIntegration().dataset_name(renamed) == StripeIntegration().dataset_name(before)
    assert StripeIntegration().dataset_name(other) != StripeIntegration().dataset_name(before)
    assert dataset_name_for_account("acct_1") == "stripe_acct_1"
