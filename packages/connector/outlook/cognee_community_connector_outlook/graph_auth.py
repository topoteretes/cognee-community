"""Microsoft Graph sign-in shared by the Microsoft connectors.

Outlook, Teams and SharePoint all talk to Microsoft Graph through one Entra ID
app, so token handling lives here rather than in a single connector.

Two ways to authenticate:

* **Delegated device-code login (default).**  MSAL prints a short code, you
  enter it at https://microsoft.com/devicelogin, and the tokens are cached in
  a private file.  Later runs refresh silently, so the login happens once.
  Works for personal Outlook.com accounts and work or school accounts alike.
* **A ready-made access token**, for quick tries.  It expires after about an
  hour and is never refreshed.
"""

import os
from typing import Any

import requests

# Microsoft's reserved scopes (offline_access, openid, profile) are added by
# MSAL on its own; passing them explicitly is an error.
MAIL_READ = "Mail.Read"
MAIL_READ_BASIC = "Mail.ReadBasic"
CALENDARS_READ = "Calendars.Read"
USER_READ = "User.Read"

# "common" accepts both personal Microsoft accounts and work or school ones.
DEFAULT_AUTHORITY = "https://login.microsoftonline.com/common"


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
        msal_app: Any = None,
    ):
        if not access_token and not client_id and msal_app is None:
            raise ValueError(
                "Microsoft Graph credentials are missing. Set OUTLOOK_CLIENT_ID to your "
                "Entra app's client id, or OUTLOOK_ACCESS_TOKEN for a quick try."
            )
        self._access_token = access_token
        self._scopes = scopes or [USER_READ]
        self._token_cache_path = os.path.expanduser(token_cache_path) if token_cache_path else None
        self._cache = None
        self._app = msal_app
        if not access_token and msal_app is None:
            self._app = self._build_app(client_id, authority)

    def _build_app(self, client_id: str, authority: str) -> Any:
        import msal

        self._cache = msal.SerializableTokenCache()
        if self._token_cache_path and os.path.exists(self._token_cache_path):
            with open(self._token_cache_path) as f:
                self._cache.deserialize(f.read())
        return msal.PublicClientApplication(client_id, authority=authority, token_cache=self._cache)

    def token(self) -> str:
        """Return an access token, logging in with a device code if needed."""
        if self._access_token:
            return self._access_token

        result = None
        accounts = self._app.get_accounts()
        if accounts:
            # Uses the cached token, or the refresh token when it has expired.
            result = self._app.acquire_token_silent(self._scopes, account=accounts[0])
        if not result:
            flow = self._app.initiate_device_flow(scopes=self._scopes)
            if "user_code" not in flow:
                raise RuntimeError(
                    f"Microsoft Graph device login could not start: {flow.get('error_description')}"
                )
            # Interactive: tells the user where to enter the code.
            print(flow["message"], flush=True)
            result = self._app.acquire_token_by_device_flow(flow)
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
            f.write(self._cache.serialize())


class GraphAuth(requests.auth.AuthBase):
    """requests auth that asks the provider for a token on every request.

    MSAL returns the cached token until it is close to expiry, so this is
    cheap, and a long sync never fails halfway on an expired token.
    """

    def __init__(self, provider: GraphTokenProvider):
        self.provider = provider

    def __call__(self, request):
        request.headers["Authorization"] = f"Bearer {self.provider.token()}"
        return request
