"""Microsoft Graph sign-in shared by the Microsoft connectors.

Outlook, Teams and SharePoint all talk to Microsoft Graph through one Entra ID
app, so token handling lives here rather than in a single connector.

Two ways to authenticate:

* **Delegated device-code login (default).**  ``login()`` prints a short code,
  you enter it at https://microsoft.com/devicelogin, and the tokens are cached
  in a private file.  Syncs only ever refresh silently from that cache, so the
  login happens once, from ``examples/login.py``.  Works for personal
  Outlook.com accounts and work or school accounts alike.
* **A ready-made access token**, for quick tries.  It expires after about an
  hour and is never refreshed.
"""

import os
from typing import Any
from urllib.parse import urlsplit

import msal
import requests

# Microsoft's reserved scopes (offline_access, openid, profile) are added by
# MSAL on its own; passing them explicitly is an error.
MAIL_READ = "Mail.Read"
MAIL_READ_BASIC = "Mail.ReadBasic"
CALENDARS_READ = "Calendars.Read"
USER_READ = "User.Read"

# "common" accepts both personal Microsoft accounts and work or school ones.
DEFAULT_AUTHORITY = "https://login.microsoftonline.com/common"
GRAPH_ORIGIN = "https://graph.microsoft.com"


class GraphLoginRequiredError(RuntimeError):
    """The token cache holds no usable sign-in, so a person has to log in again."""


class GraphHostError(requests.exceptions.InvalidURL):
    """A request was about to carry the Graph token to a host other than Graph."""


class GraphTokenProvider:
    """Hands out a valid Graph access token, refreshing it when needed."""

    def __init__(
        self,
        *,
        client_id: str | None = None,
        scopes: list[str] | None = None,
        authority: str = DEFAULT_AUTHORITY,
        token_cache_path: str | None = None,
        access_token: str | None = None,
        account: str | None = None,
        msal_app: Any = None,
    ):
        if not access_token and not client_id and msal_app is None:
            raise ValueError(
                "Microsoft Graph credentials are missing. Pass client_id, your Entra "
                "app's client id, or access_token for a quick try."
            )
        self._access_token = access_token
        self._client_id = client_id
        self._authority = authority
        self._scopes = scopes or [USER_READ]
        self._token_cache_path = os.path.expanduser(token_cache_path) if token_cache_path else None
        self._account = account
        self._cache = None
        # Built on the first token request: MSAL looks the tenant up over the
        # network as soon as the app is created.
        self._app = msal_app

    def _get_app(self) -> Any:
        if self._app is None:
            self._cache = msal.SerializableTokenCache()
            if self._token_cache_path and os.path.exists(self._token_cache_path):
                with open(self._token_cache_path) as f:
                    self._cache.deserialize(f.read())
            self._app = msal.PublicClientApplication(
                self._client_id, authority=self._authority, token_cache=self._cache
            )
        return self._app

    def _cached_account(self, app: Any) -> dict | None:
        accounts = app.get_accounts(username=self._account) if self._account else app.get_accounts()
        if len(accounts) > 1:
            raise ValueError(
                "The token cache holds more than one Microsoft account. Pass account= with the "
                "address to sync, or give each mailbox its own token_cache_path."
            )
        return accounts[0] if accounts else None

    def token(self) -> str:
        """Return an access token from the cache, refreshing it silently.

        Never starts an interactive login: a sync must not wait for a person.
        """
        if self._access_token:
            return self._access_token
        app = self._get_app()
        account = self._cached_account(app)
        # Uses the cached token, or the refresh token when it has expired.
        result = app.acquire_token_silent(self._scopes, account=account) if account else None
        if not result or "access_token" not in result:
            raise GraphLoginRequiredError(
                "Microsoft Graph sign-in is needed: run examples/login.py with the same "
                "client id and token_cache_path, then sync again."
            )
        self._save_cache()
        return result["access_token"]

    def login(self) -> str:
        """Sign in interactively with a device code and cache the tokens."""
        if self._access_token:
            return self._access_token
        app = self._get_app()
        flow = app.initiate_device_flow(scopes=self._scopes)
        if "user_code" not in flow:
            raise RuntimeError(
                f"Microsoft Graph device login could not start: {flow.get('error_description')}"
            )
        # Interactive: tells the user where to enter the code.
        print(flow["message"], flush=True)
        result = app.acquire_token_by_device_flow(flow)
        if "access_token" not in result:
            raise RuntimeError(
                "Microsoft Graph sign-in failed: "
                f"{result.get('error')}: {result.get('error_description')}"
            )
        self._save_cache()
        return result["access_token"]

    def _save_cache(self) -> None:
        if not (self._cache is not None and self._token_cache_path):
            return
        if not self._cache.has_state_changed:
            return
        # The cache holds a refresh token, so write it private (0600).
        fd = os.open(self._token_cache_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with open(fd, "w") as f:
            # The mode above only applies to a new file; an existing one keeps its own.
            os.chmod(self._token_cache_path, 0o600)
            f.write(self._cache.serialize())


def _origin(url: str) -> tuple[str, str, int | None]:
    parts = urlsplit(url)
    scheme = parts.scheme.lower()
    return (
        scheme,
        (parts.hostname or "").lower(),
        parts.port or {"https": 443, "http": 80}.get(scheme),
    )


class GraphAuth(requests.auth.AuthBase):
    """requests auth that asks the provider for a token on every request.

    MSAL returns the cached token until it is close to expiry, so this is
    cheap, and a long sync never fails halfway on an expired token.  The token
    only goes to the Graph host the client was built for: a next-page link
    that points anywhere else is refused before it is sent.
    """

    def __init__(self, provider: GraphTokenProvider, base_url: str = GRAPH_ORIGIN):
        self.provider = provider
        self._origin = _origin(base_url)

    def __call__(self, request):
        if _origin(request.url) != self._origin:
            raise GraphHostError("Refusing to send the Microsoft Graph token to another host.")
        request.headers["Authorization"] = f"Bearer {self.provider.token()}"
        return request
