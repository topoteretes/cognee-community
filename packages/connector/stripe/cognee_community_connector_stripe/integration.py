"""Stripe Connect as an ``OAuthIntegration`` for cognee's generic integrations API.

Plugs the connector into the connect story cognee core already ships: the
``/api/v1/integrations/stripe/*`` routes mint the consent URL, exchange the
code, store the connection, show a picker and run a sync. Nothing here
re-implements that flow; this module only supplies the Stripe-specific
pieces. Registration is opt-in (``import cognee_community_connector_stripe.register``)
so importing the connector never touches the integrations registry.

* **OAuth**: Stripe Connect's authorization-code flow with
  ``scope=read_only``. The code is exchanged with the platform's secret key
  as HTTP Basic auth, and a connection that comes back with any other scope is
  refused. Stripe deprecates the per-account access and refresh tokens in
  favour of the platform key plus a ``Stripe-Account`` header, so the
  connector keeps neither: the connection is the connected account id, and no
  user ever pastes a secret key.
* **Resource picker**: ``list_resources`` offers the object types (customers,
  subscriptions, invoices, charges, products, disputes). The selection is
  three-state, as the generic picker defines it: ``null`` syncs every type,
  ``[]`` syncs nothing (and forgets what was synced), and a list syncs those
  types. A new connection starts with nothing selected.
* **Sync**: ``sync_now`` runs :func:`stripe_source` for the stored selection
  into one dataset per connected account, through core's ``run_sync`` so the
  generic routes see it in flight.
* **Disconnect**: ``revoke_remote`` deauthorizes the account at Stripe, so
  the platform loses access to it, not just the local record.
"""

import base64
import re
from typing import Any
from urllib.parse import urlencode

import aiohttp
from cognee.modules.integrations.base import (
    IntegrationSettings,
    OAuthInstallation,
    OAuthIntegration,
)
from cognee.modules.integrations.ingestion import extraction_checkpoint, run_sync
from cognee.modules.integrations.models.IntegrationCredential import IntegrationCredential
from cognee.shared.logging_utils import get_logger
from pydantic_settings import SettingsConfigDict

from cognee_community_connector_stripe.stripe import OBJECT_TYPES, stripe_source

logger = get_logger("stripe_integration")

STRIPE_PROVIDER = "stripe"
STRIPE_DATASET_PREFIX = "stripe"
SELECTION_KEY = "selected_objects"
SYNC_STATUS_OK = "ok"
SYNC_STATUS_DEGRADED = "degraded"

_AUTHORIZE_URL = "https://connect.stripe.com/oauth/authorize"
_TOKEN_URL = "https://connect.stripe.com/oauth/token"
_DEAUTHORIZE_URL = "https://connect.stripe.com/oauth/deauthorize"
_ACCOUNT_URL = "https://api.stripe.com/v1/accounts/{account_id}"

_SCOPE = "read_only"
_TIMEOUT = aiohttp.ClientTimeout(total=30)

_RESOURCE_LABELS = {
    "customers": ("Customers", "Customer names, balance and status."),
    "subscriptions": ("Subscriptions", "Plans, status, trials and cancellations."),
    "invoices": ("Invoices", "Amounts, status, due dates and line items."),
    "charges": ("Charges", "Payments, refunds and failures, without card details."),
    "products": ("Products", "What you sell and whether it is active."),
    "disputes": ("Disputes", "Reason, status and evidence deadline."),
}


class StripeSettings(IntegrationSettings):
    """Configuration for the Stripe Connect integration.

    One Connect platform serves every connecting user; these values identify
    it. They default to empty strings rather than failing at import so
    deployments without Stripe configured (and unit tests) still boot —
    consumers call :func:`require` at use time instead.

    ``client_id`` is the platform's Connect client id (``ca_...``) and
    ``client_secret`` its secret API key (``sk_...``), which Stripe uses both
    to exchange codes and to read connected accounts. ``redirect_uri`` and
    ``frontend_base_url`` come from :class:`IntegrationSettings`.
    """

    model_config = SettingsConfigDict(env_prefix="STRIPE_", extra="ignore")

    # Signs the OAuth state parameter. Env: STRIPE_STATE_SECRET
    state_secret: str = ""
    # Optional ISO date: keep only invoices, charges and disputes created on
    # or after it. Env: STRIPE_CREATED_SINCE
    created_since: str = ""


stripe_settings = StripeSettings()


def require(field_name: str) -> str:
    """Return a settings value, refusing to proceed when it is unset.

    A missing value must never degrade into a token exchange with an empty
    key or a state signed with an empty secret — both would fail in
    confusing, downstream ways instead of naming the actual problem.
    """
    value = getattr(stripe_settings, field_name)
    if not value:
        raise RuntimeError(f"STRIPE_{field_name.upper()} is not configured")
    return value


class StripeIntegration(OAuthIntegration):
    provider = STRIPE_PROVIDER
    settings_cls = StripeSettings
    resource_selection_key = SELECTION_KEY

    def __init__(self, http_client: Any = None):
        # stripe HTTP client for the API reads (mainly a test-injection
        # point); None uses the stripe library's default.
        self._http_client = http_client

    def authorize_url(self, state: str) -> str:
        params = {
            "response_type": "code",
            "client_id": require("client_id"),
            "scope": _SCOPE,
            "redirect_uri": require("redirect_uri"),
            "state": state,
        }
        return f"{_AUTHORIZE_URL}?{urlencode(params)}"

    async def exchange_code(self, code: str) -> dict[str, Any]:
        """Exchange the OAuth code (single use, valid for 5 minutes)."""
        payload = await _post_form(_TOKEN_URL, {"grant_type": "authorization_code", "code": code})
        if not payload.get("stripe_user_id"):
            raise RuntimeError("Stripe code exchange returned no stripe_user_id")
        if payload.get("scope") != _SCOPE:
            # The consent screen asked for read_only; anything broader is not
            # what the user agreed to here, so the connection is not kept.
            raise RuntimeError(f"Stripe granted scope {payload.get('scope')!r}, not {_SCOPE!r}")
        return payload

    async def exchange_callback(self, code: str, params: dict[str, str]) -> dict[str, Any]:
        """Exchange the code, then attach the connected account's display name.

        ``parse_installation`` is sync, so the one ``/v1/accounts`` call that
        names the account for the integrations UI is made here.
        """
        token_response = await self.exchange_code(code)
        account = await _get_account(token_response["stripe_user_id"])
        return {**token_response, "account": account}

    def parse_installation(self, token_response: dict[str, Any]) -> OAuthInstallation:
        account_id = token_response.get("stripe_user_id")
        if not account_id:
            raise ValueError("Stripe token response carries no stripe_user_id")
        account = token_response.get("account") or {}
        display_name = (
            ((account.get("settings") or {}).get("dashboard") or {}).get("display_name")
            or (account.get("business_profile") or {}).get("name")
            or None
        )
        return OAuthInstallation(
            provider_account_id=str(account_id),
            # The deprecated access and refresh tokens are dropped on purpose:
            # reads use the platform key with the Stripe-Account header, so
            # nothing secret is stored per connection.
            token_payload={"stripe_user_id": str(account_id)},
            provider_metadata={
                "display_name": display_name,
                "livemode": bool(token_response.get("livemode")),
                # A new connection must opt into object types before importing
                # data. complete_installation preserves choices on reconnect.
                SELECTION_KEY: [],
            },
            account_label=display_name or str(account_id),
            scopes=_SCOPE,
            token_expires_at=None,
            auth_type="oauth2",
        )

    def state_signing_secret(self) -> str:
        return require("state_secret")

    def frontend_base_url(self) -> str:
        return require("frontend_base_url")

    async def on_installed(self, credential: IntegrationCredential) -> None:
        """Connecting stores authorization only; sync starts once types are picked."""
        return

    async def list_resources(self, credential: IntegrationCredential) -> list[dict[str, Any]]:
        """The object types a user can choose to ingest."""
        return [
            {
                "id": name,
                "name": _RESOURCE_LABELS[name][0],
                "description": _RESOURCE_LABELS[name][1],
                "attributes": {},
            }
            for name in OBJECT_TYPES
        ]

    async def sync_now(self, credential: IntegrationCredential) -> None:
        await sync_stripe(credential, http_client=self._http_client)

    def dataset_name(self, credential: IntegrationCredential) -> str:
        return dataset_name_for_account(str(credential.provider_account_id))

    async def revoke_remote(self, credential: IntegrationCredential) -> None:
        """Deauthorize the connected account at Stripe."""
        await _post_form(
            _DEAUTHORIZE_URL,
            {
                "client_id": require("client_id"),
                "stripe_user_id": str(credential.provider_account_id),
            },
        )


# ---------------------------------------------------------------------------
# Sync
# ---------------------------------------------------------------------------


def dataset_name_for_account(account_id: str) -> str:
    """The account's dataset, named from its id so a renamed account keeps its data."""
    return f"{STRIPE_DATASET_PREFIX}_{re.sub(r'[^A-Za-z0-9_]+', '_', account_id).lower()}"


def selected_objects(credential: IntegrationCredential) -> list[str]:
    """The stored selection, as a list of object types (``null`` means all)."""
    selected = (credential.provider_metadata or {}).get(SELECTION_KEY)
    if selected is None:
        return list(OBJECT_TYPES)
    if not isinstance(selected, list) or any(name not in OBJECT_TYPES for name in selected):
        raise ValueError(f"Stripe selection must be a list of {sorted(OBJECT_TYPES)} or null")
    return selected


async def sync_stripe(credential: IntegrationCredential, http_client: Any = None) -> None:
    """Sync the account's selected object types into its dataset and record the outcome."""
    from cognee.api.v1.remember.remember import remember
    from cognee.modules.users.methods import get_user

    async def _sync(
        credential: IntegrationCredential, counts: dict[str, int]
    ) -> tuple[str, dict[str, int]]:
        # An empty selection still runs: its empty snapshot tombstones
        # whatever an earlier sync stored.
        source = stripe_source(
            require("client_secret"),
            account_id=str(credential.provider_account_id),
            objects=selected_objects(credential),
            created_since=stripe_settings.created_since or None,
            check_active=extraction_checkpoint(credential),
            http_client=http_client,
        )
        owner = await get_user(credential.user_id)
        result = await remember(
            source,
            dataset_name=StripeIntegration().dataset_name(credential),
            user=owner,
            self_improvement=False,
            primary_key="id",
            write_disposition="merge",
        )
        stats = source.cognee_sync_stats
        counts.update(scanned=stats.get("written", 0), deleted=stats.get("deleted", 0))
        if getattr(result, "status", None) == "errored":
            counts["failed"] = 1
            return SYNC_STATUS_DEGRADED, counts
        return SYNC_STATUS_OK, counts

    await run_sync(STRIPE_PROVIDER, credential, _sync)


# ---------------------------------------------------------------------------
# OAuth plumbing
# ---------------------------------------------------------------------------


def _basic_auth() -> dict[str, str]:
    """The platform's secret key as HTTP Basic auth, as Stripe's OAuth endpoints take it."""
    # Built by hand: aiohttp deprecates BasicAuth, and its replacement
    # (encode_basic_auth) is newer than the oldest aiohttp cognee allows.
    credentials = f"{require('client_secret')}:".encode()
    return {"Authorization": f"Basic {base64.b64encode(credentials).decode()}"}


async def _post_form(url: str, form: dict[str, str]) -> dict[str, Any]:
    """POST to a Stripe Connect OAuth endpoint with the platform key."""
    async with (
        aiohttp.ClientSession(timeout=_TIMEOUT) as session,
        session.post(url, data=form, headers=_basic_auth()) as response,
    ):
        if response.status != 200:
            # Never echo the body: it can carry the code back.
            raise RuntimeError(f"Stripe OAuth request to {url} failed: HTTP {response.status}")
        return await response.json()


async def _get_account(account_id: str) -> dict[str, Any]:
    """The connected account, read with the platform key. Best effort: only labels it."""
    url = _ACCOUNT_URL.format(account_id=account_id)
    try:
        async with (
            aiohttp.ClientSession(timeout=_TIMEOUT) as session,
            session.get(url, headers=_basic_auth()) as response,
        ):
            if response.status == 200:
                return await response.json()
            logger.warning("Stripe /v1/accounts lookup failed: HTTP %s", response.status)
    except (TimeoutError, aiohttp.ClientError):
        logger.warning("Stripe /v1/accounts lookup failed", exc_info=True)
    return {}
