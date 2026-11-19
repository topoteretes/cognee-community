"""Figma as an ``OAuthIntegration`` for cognee's generic integrations API.

Plugs the connector into the connect story cognee core already ships: the
``/api/v1/integrations/figma/*`` routes mint the consent URL, exchange the
code, store the token encrypted, show a folder picker and run a sync. Nothing
here re-implements that flow; this module only supplies the Figma-specific
pieces. Registration is opt-in (``import cognee_community_connector_figma.register``)
so importing the connector never touches the integrations registry.

* **OAuth2**: the authorization-code flow against ``www.figma.com/oauth``.
  Figma takes the app credentials as an HTTP Basic header on both the token
  and the refresh endpoint, not in the form body. Access tokens last 90 days
  and the refresh token never rotates, so ``refresh`` keeps the stored one.
* **Resource picker**: ``list_resources`` returns the folders (formerly
  "projects") of the teams in ``FIGMA_TEAM_IDS``. Figma has no API that lists
  a user's teams, so the deployment names them. Listing folders needs the
  ``folders:read`` scope, which Figma grants only to **private** OAuth apps.
* **Sync**: ``sync_now`` runs :func:`figma_source` for the stored selection
  into one dataset per connected account. The selection is three-state, as
  the generic picker defines it: ``null`` syncs every folder of the
  configured teams, ``[]`` syncs nothing (and forgets what was synced), and
  a list syncs those folders.

``revoke_remote`` stays the base no-op: Figma documents no token-revoke
endpoint, so disconnecting only revokes the local credential; the user can
remove the app under *Settings → Security → Connected apps* in Figma.
"""

import asyncio
import base64
import logging
import re
from datetime import datetime, timedelta, timezone
from hashlib import sha256
from typing import Any
from urllib.parse import urlencode

import aiohttp
from cognee.modules.integrations.base import (
    IntegrationSettings,
    OAuthInstallation,
    OAuthIntegration,
)
from cognee.modules.integrations.credentials import (
    decrypt_token_payload,
    require_active_credential,
    update_refreshed_credential,
)
from cognee.modules.integrations.ingestion import (
    empty_resource,
    extraction_checkpoint,
    run_sync,
    source_counts,
)
from cognee.modules.integrations.models.IntegrationCredential import IntegrationCredential
from pydantic_settings import SettingsConfigDict

from cognee_community_connector_figma.figma import (
    DEFAULT_RESOURCE_NAME,
    comments_table,
    figma_client_config,
    figma_source,
    files_table,
    list_folders,
)

logger = logging.getLogger(__name__)

FIGMA_PROVIDER = "figma"
FIGMA_DATASET_PREFIX = "figma"
SYNC_STATUS_OK = "ok"
SYNC_STATUS_DEGRADED = "degraded"

_AUTHORIZE_URL = "https://www.figma.com/oauth"
_TOKEN_URL = "https://api.figma.com/v1/oauth/token"
_REFRESH_URL = "https://api.figma.com/v1/oauth/refresh"
_ME_URL = "https://api.figma.com/v1/me"

# Read-only, and only what the connector calls: who connected (current_user),
# the folder picker and listing (folders), node trees (file_content) and
# comment threads (file_comments). The app must have these selected in its
# Figma settings; the request may ask for a subset of them, never more.
_SCOPES = "current_user:read file_content:read file_comments:read folders:read"

_TIMEOUT = aiohttp.ClientTimeout(total=30)

# Refresh this far before the token actually dies, so a long sync does not
# expire halfway through its file list.
_REFRESH_MARGIN = timedelta(minutes=5)


class FigmaSettings(IntegrationSettings):
    """Configuration for the Figma integration.

    One Figma OAuth app serves every connecting user; these values identify
    that app. They default to empty strings rather than failing at import so
    deployments without Figma configured (and unit tests) still boot —
    consumers call :func:`require` at use time instead.

    ``client_id``/``client_secret``/``redirect_uri``/``frontend_base_url``
    come from :class:`IntegrationSettings`; the fields below are what Figma
    needs beyond that shape.
    """

    model_config = SettingsConfigDict(env_prefix="FIGMA_", extra="ignore")

    # Signs the OAuth state parameter. Figma sends no signed webhooks on this
    # path, so this is its only use. Env: FIGMA_STATE_SECRET
    state_secret: str = ""
    # Comma-separated ids of the teams whose folders the picker offers. Figma
    # cannot list a user's teams; the id is in the team page URL after
    # /team/. Env: FIGMA_TEAM_IDS
    team_ids: str = ""


figma_settings = FigmaSettings()


def require(field_name: str) -> str:
    """Return a settings value, refusing to proceed when it is unset.

    A missing value must never degrade into a token exchange with empty app
    credentials or a state signed with an empty key — both would fail in
    confusing, downstream ways instead of naming the actual problem.
    """
    value = getattr(figma_settings, field_name)
    if not value:
        raise RuntimeError(f"FIGMA_{field_name.upper()} is not configured")
    return value


def configured_team_ids() -> list[str]:
    """The team ids from ``FIGMA_TEAM_IDS``; at least one is required."""
    team_ids = [team_id.strip() for team_id in require("team_ids").split(",")]
    return [team_id for team_id in team_ids if team_id]


class FigmaIntegration(OAuthIntegration):
    provider = FIGMA_PROVIDER
    settings_cls = FigmaSettings
    resource_selection_key = "selected_folder_ids"

    def __init__(self, session: Any = None):
        # requests session for the Figma REST calls made through dlt (mainly a
        # test-injection point); None uses dlt's retrying session.
        self._session = session

    def authorize_url(self, state: str) -> str:
        params = {
            "client_id": require("client_id"),
            "redirect_uri": require("redirect_uri"),
            "scope": _SCOPES,
            "state": state,
            "response_type": "code",
        }
        return f"{_AUTHORIZE_URL}?{urlencode(params)}"

    async def exchange_code(self, code: str) -> dict[str, Any]:
        """Exchange the OAuth code (valid for 30 seconds) for the user's tokens."""
        payload = await _post_token_request(
            _TOKEN_URL,
            {
                "redirect_uri": require("redirect_uri"),
                "code": code,
                "grant_type": "authorization_code",
            },
        )
        if not payload.get("access_token"):
            raise RuntimeError("Figma code exchange returned no access_token")
        return payload

    async def exchange_callback(self, code: str, params: dict[str, str]) -> dict[str, Any]:
        """Exchange the code, then attach the profile of who authorized.

        The token response names the user only by id, but the connection is
        shown by email in the integrations UI, and ``parse_installation`` is
        sync so it cannot ask. The fresh token is spent here, in the async
        leg, on one ``/v1/me`` call and the result rides along — the same
        shape as the Linear and Google Drive adapters.
        """
        token_response = await self.exchange_code(code)
        me = await _get_me(token_response["access_token"])
        return {**token_response, "me": me}

    def parse_installation(self, token_response: dict[str, Any]) -> OAuthInstallation:
        me = token_response.get("me") or {}
        # user_id_string is the stable id; the numeric user_id is deprecated.
        account_id = token_response.get("user_id_string") or me.get("id")
        if not account_id:
            raise ValueError("Figma token response carries no user id")

        # Secret material stays in token_payload (encrypted at rest).
        token_payload = {"access_token": token_response["access_token"]}
        if token_response.get("refresh_token"):
            token_payload["refresh_token"] = token_response["refresh_token"]

        return OAuthInstallation(
            provider_account_id=str(account_id),
            token_payload=token_payload,
            provider_metadata={
                "email": me.get("email"),
                "handle": me.get("handle"),
                # A new connection must opt into folders before importing data.
                # complete_installation preserves existing choices on reconnect.
                "selected_folder_ids": [],
            },
            account_label=me.get("email") or me.get("handle"),
            scopes=_SCOPES,
            token_expires_at=_expires_at(token_response.get("expires_in")),
            auth_type="oauth2",
        )

    def state_signing_secret(self) -> str:
        return require("state_secret")

    def frontend_base_url(self) -> str:
        return require("frontend_base_url")

    async def on_installed(self, credential: IntegrationCredential) -> None:
        """Connecting stores authorization only; sync starts once folders are picked."""
        return

    async def list_resources(self, credential: IntegrationCredential) -> list[dict[str, Any]]:
        """Every folder (and subfolder) of the configured teams the user can see."""
        token = await access_token_for(credential)
        client_config = figma_client_config(token, session=self._session)
        folders = await asyncio.to_thread(list_folders, client_config, configured_team_ids())
        return [
            {
                "id": str(folder["id"]),
                "name": str(folder.get("name") or "Untitled folder"),
                "description": None,
                "attributes": {
                    "team_id": folder.get("_teams_id"),
                    "parent_folder_id": folder.get("parent_folder_id"),
                },
            }
            for folder in folders
        ]

    async def sync_now(self, credential: IntegrationCredential) -> None:
        await sync_figma(credential, session=self._session)

    def dataset_name(self, credential: IntegrationCredential) -> str:
        metadata = credential.provider_metadata or {}
        return dataset_name_for_account(
            str(metadata.get("email") or metadata.get("handle") or ""),
            str(credential.provider_account_id),
        )

    async def refresh(self, credential: IntegrationCredential) -> None:
        """Rotate the access token in place.

        Figma keeps one access token per app and user, so the refreshed token
        invalidates the old one at once; it is written back before anything
        else uses the connection. The refresh token itself does not rotate.
        """
        token_payload = decrypt_token_payload(credential)
        refresh_token = token_payload.get("refresh_token")
        if not refresh_token:
            raise RuntimeError(
                f"Figma credential for user {credential.provider_account_id} holds no "
                f"refresh token; the account must reconnect"
            )

        refreshed = await _post_token_request(_REFRESH_URL, {"refresh_token": refresh_token})
        if not refreshed.get("access_token"):
            raise RuntimeError("Figma token refresh returned no access_token")

        await update_refreshed_credential(
            credential,
            token_payload={
                "access_token": refreshed["access_token"],
                "refresh_token": refresh_token,
            },
            token_expires_at=_expires_at(refreshed.get("expires_in")),
            scopes=credential.scopes,
        )


# ---------------------------------------------------------------------------
# Sync
# ---------------------------------------------------------------------------


def dataset_name_for_account(label: str, account_id: str = "") -> str:
    """Keep the account's dataset stable and disambiguate slug collisions."""
    slug = re.sub(r"[^A-Za-z0-9_]+", "_", label or "").strip("_").lower()
    name = f"{FIGMA_DATASET_PREFIX}_{slug or 'account'}"
    return f"{name}_{sha256(account_id.encode()).hexdigest()[:10]}" if account_id else name


async def sync_figma(credential: IntegrationCredential, session: Any = None) -> None:
    """Sync the account's selected folders into its dataset and record the outcome.

    Goes through core's ``run_sync``, which allows one sync per account at a
    time and stamps how it went on the connection.
    """
    from cognee.api.v1.remember.remember import remember
    from cognee.modules.users.methods import get_user

    async def _sync(
        credential: IntegrationCredential, counts: dict[str, int]
    ) -> tuple[str, dict[str, int]]:
        selected = (credential.provider_metadata or {}).get("selected_folder_ids")
        if selected is not None and not (
            isinstance(selected, list)
            and all(isinstance(folder_id, str) and folder_id for folder_id in selected)
        ):
            raise ValueError("Figma selection must be a list of folder ids or null")

        owner = await get_user(credential.user_id)
        dataset = FigmaIntegration().dataset_name(credential)
        if selected == []:
            await _forget_synced(credential, owner, dataset)
            return SYNC_STATUS_OK, counts

        source = figma_source(
            await access_token_for(credential),
            team_ids=configured_team_ids() if selected is None else None,
            folder_ids=selected,
            check_active=extraction_checkpoint(credential),
            session=session,
        )
        try:
            result = await remember(
                source, dataset_name=dataset, user=owner, self_improvement=False
            )
        finally:
            # Also when the run raised, so the connection says how far it got
            # and why it stopped (failed_budget or failed_rate_limit).
            counts.update(source_counts(source))
        if counts.get("failed") or getattr(result, "status", None) == "errored":
            counts["failed"] = max(1, counts.get("failed", 0))
            return SYNC_STATUS_DEGRADED, counts
        return SYNC_STATUS_OK, counts

    await run_sync(FIGMA_PROVIDER, credential, _sync)


async def _forget_synced(credential: IntegrationCredential, owner: Any, dataset: str) -> None:
    """Empty both staging tables, so an emptied selection forgets what was synced.

    ``figma_source`` refuses an empty selection (a typo must not purge a
    dataset), so a deliberately empty one is an explicit replace of each table
    with nothing, the way core's own integrations retire a deselected scope.
    """
    from cognee.api.v1.remember.remember import remember

    for table in (files_table(DEFAULT_RESOURCE_NAME), comments_table(DEFAULT_RESOURCE_NAME)):
        result = await remember(
            empty_resource(FIGMA_PROVIDER, table, extraction_checkpoint(credential)),
            dataset_name=dataset,
            user=owner,
            write_disposition="replace",
            primary_key="id",
            max_rows_per_table=0,
            self_improvement=False,
        )
        if getattr(result, "status", None) == "errored":
            raise RuntimeError("Failed to forget the Figma data of an emptied selection")


# ---------------------------------------------------------------------------
# OAuth plumbing
# ---------------------------------------------------------------------------


async def access_token_for(credential: IntegrationCredential) -> str:
    """A usable access token for this credential, refreshed if it is about to die.

    The only sanctioned path from a stored credential to a bearer token, so
    no caller has to repeat the expiry check.
    """
    credential = await require_active_credential(credential)
    expires_at = credential.token_expires_at
    if expires_at is not None:
        # SQLite stores no timezone; the value is written as UTC.
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=timezone.utc)
        if expires_at - _REFRESH_MARGIN <= datetime.now(timezone.utc):
            await FigmaIntegration().refresh(credential)
            # refresh() writes through its own session, so the instance we
            # were handed still carries the old ciphertext. Read it back.
            credential = await require_active_credential(credential)

    token = decrypt_token_payload(credential).get("access_token")
    if not token:
        raise RuntimeError(
            f"Figma credential for user {credential.provider_account_id} holds no access token"
        )
    return token


async def _post_token_request(url: str, form: dict[str, str]) -> dict[str, Any]:
    """POST to a Figma OAuth endpoint with the app credentials as Basic auth."""
    # Built by hand: aiohttp deprecates BasicAuth, and its replacement
    # (encode_basic_auth) is newer than the oldest aiohttp cognee allows.
    credentials = f"{require('client_id')}:{require('client_secret')}".encode()
    headers = {"Authorization": f"Basic {base64.b64encode(credentials).decode()}"}
    async with (
        aiohttp.ClientSession(timeout=_TIMEOUT) as session,
        session.post(url, data=form, headers=headers) as response,
    ):
        if response.status != 200:
            # Never echo the body: it can carry the code or token back.
            raise RuntimeError(f"Figma OAuth request to {url} failed: HTTP {response.status}")
        return await response.json()


async def _get_me(access_token: str) -> dict[str, Any]:
    """The profile (id, handle, email) of the user a token belongs to."""
    async with (
        aiohttp.ClientSession(timeout=_TIMEOUT) as session,
        session.get(_ME_URL, headers={"Authorization": f"Bearer {access_token}"}) as response,
    ):
        if response.status != 200:
            raise RuntimeError(f"Figma /v1/me failed: HTTP {response.status}")
        return await response.json()


def _expires_at(expires_in: Any) -> datetime | None:
    if not expires_in:
        return None
    return datetime.now(timezone.utc) + timedelta(seconds=int(expires_in))
