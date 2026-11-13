"""Google OAuth 2.0 for Search Console: settings, token refresh and the one-time sign-in.

Three ways in, in this order: a ready-made access token, a refresh token with a client id and
secret (headless), or a token file written by :func:`authorize` (local, browser consent once).
Only the read-only ``webmasters.readonly`` scope is ever requested.
"""

import json
import os
from collections.abc import Mapping
from typing import Any

import httpx
from cognee.modules.integrations.google import GoogleSettings
from pydantic_settings import SettingsConfigDict

SEARCH_CONSOLE_SCOPE = "https://www.googleapis.com/auth/webmasters.readonly"
TOKEN_URI = "https://oauth2.googleapis.com/token"
DEFAULT_TOKEN_PATH = "~/.cognee_google_search_console_token.json"


class GoogleSearchConsoleSettings(GoogleSettings):
    """cognee's Google OAuth settings, read from ``GOOGLE_SEARCH_CONSOLE_*``.

    Inherits ``client_id`` and ``client_secret`` from cognee's ``GoogleSettings`` and adds what
    a local or headless sync needs.
    """

    model_config = SettingsConfigDict(env_prefix="GOOGLE_SEARCH_CONSOLE_", extra="ignore")

    access_token: str = ""
    refresh_token: str = ""
    credentials_path: str = ""
    token_path: str = ""


class SearchConsoleAuthError(Exception):
    """Google refused to issue or refresh an access token.

    Carries only Google's short error code (``invalid_grant`` ...), never a response body
    or a token.
    """

    def __init__(self, code: str):
        super().__init__(
            f"Google refused the sign-in ({code}). Run authorize() again, or check the client id, "
            "client secret and refresh token."
        )
        self.code = code


class SearchConsoleCredentials:
    """An access token that can renew itself when a refresh token and client are known."""

    def __init__(
        self,
        *,
        access_token: str | None = None,
        refresh_token: str | None = None,
        client_id: str | None = None,
        client_secret: str | None = None,
        token_uri: str = TOKEN_URI,
        transport: httpx.BaseTransport | None = None,
    ):
        # A pasted token with a stray newline would make the HTTP library fail with the token
        # in its message, so refuse it without echoing it.
        for name, value in (("access token", access_token), ("refresh token", refresh_token)):
            if value and (value != value.strip() or any(c.isspace() for c in value)):
                raise ValueError(
                    f"The {name} contains whitespace; paste it without spaces or newlines."
                )
        self._access_token = access_token or None
        self._refresh_token = refresh_token or None
        self._client_id = client_id or None
        self._client_secret = client_secret or None
        self._token_uri = token_uri
        self._transport = transport
        if not (self._access_token or self.can_refresh):
            raise ValueError(
                "Pass an access token, or a refresh token with a client id and secret."
            )

    @property
    def can_refresh(self) -> bool:
        return bool(self._refresh_token and self._client_id and self._client_secret)

    def token(self) -> str:
        """The current access token, fetching one first if there is none yet."""
        if not self._access_token:
            self.refresh()
        assert self._access_token is not None
        return self._access_token

    def refresh(self) -> None:
        """Exchange the refresh token for a new access token."""
        if not self.can_refresh:
            raise SearchConsoleAuthError("token_expired")
        data = {
            "grant_type": "refresh_token",
            "refresh_token": self._refresh_token,
            "client_id": self._client_id,
            "client_secret": self._client_secret,
        }
        try:
            with httpx.Client(transport=self._transport, timeout=30.0) as http:
                response = http.post(self._token_uri, data=data)
        except httpx.TransportError as exc:
            raise SearchConsoleAuthError(type(exc).__name__) from None
        try:
            body = response.json()
        except ValueError:
            body = {}
        token = body.get("access_token") if isinstance(body, dict) else None
        if response.status_code != 200 or not token:
            error = body.get("error") if isinstance(body, dict) else None
            raise SearchConsoleAuthError(str(error or f"http_{response.status_code}"))
        self._access_token = token


def credentials_from_token_file(path: str) -> SearchConsoleCredentials:
    """Load the file :func:`authorize` wrote (Google's authorized-user JSON)."""
    with open(os.path.expanduser(path), encoding="utf-8") as token_file:
        info: Mapping[str, Any] = json.load(token_file)
    return SearchConsoleCredentials(
        access_token=info.get("token"),
        refresh_token=info.get("refresh_token"),
        client_id=info.get("client_id"),
        client_secret=info.get("client_secret"),
        token_uri=info.get("token_uri") or TOKEN_URI,
    )


def authorize(
    credentials_path: str | None = None, token_path: str | None = None
) -> SearchConsoleCredentials:
    """Sign in once through the browser and cache the token for later runs.

    Opens Google's consent screen (installed-app flow) for the read-only scope and writes the
    token, including its long-lived refresh token, to ``token_path`` with mode 0600. Later runs
    reuse and silently renew it. Needs an OAuth client of type "Desktop app"; its JSON is
    ``credentials_path``.
    """
    settings = GoogleSearchConsoleSettings()
    credentials_path = os.path.expanduser(credentials_path or settings.credentials_path)
    token_path = os.path.expanduser(token_path or settings.token_path or DEFAULT_TOKEN_PATH)
    if not credentials_path or not os.path.exists(credentials_path):
        raise FileNotFoundError(
            f"Google OAuth client secrets not found at '{credentials_path}'. Download an OAuth "
            "client (Desktop app) from the Google Cloud Console and set "
            "GOOGLE_SEARCH_CONSOLE_CREDENTIALS_PATH."
        )
    from google_auth_oauthlib.flow import InstalledAppFlow

    flow = InstalledAppFlow.from_client_secrets_file(credentials_path, [SEARCH_CONSOLE_SCOPE])
    creds = flow.run_local_server(port=0)
    # The token file holds the refresh token: keep it private, even over an old file.
    fd = os.open(token_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as token_file:
        token_file.write(creds.to_json())
    os.chmod(token_path, 0o600)
    return credentials_from_token_file(token_path)


def resolve_credentials(
    access_token: str | None = None, settings: GoogleSearchConsoleSettings | None = None
) -> SearchConsoleCredentials:
    """Pick credentials: an access token, else a refresh token, else the cached token file."""
    settings = settings or GoogleSearchConsoleSettings()
    access_token = access_token or settings.access_token
    if access_token:
        return SearchConsoleCredentials(access_token=access_token)
    if settings.refresh_token:
        return SearchConsoleCredentials(
            refresh_token=settings.refresh_token,
            client_id=settings.client_id,
            client_secret=settings.client_secret,
        )
    token_path = os.path.expanduser(settings.token_path or DEFAULT_TOKEN_PATH)
    if os.path.exists(token_path):
        return credentials_from_token_file(token_path)
    raise ValueError(
        "Google Search Console credentials are missing. Run authorize() once (needs "
        "GOOGLE_SEARCH_CONSOLE_CREDENTIALS_PATH), or set GOOGLE_SEARCH_CONSOLE_REFRESH_TOKEN with "
        "GOOGLE_SEARCH_CONSOLE_CLIENT_ID and GOOGLE_SEARCH_CONSOLE_CLIENT_SECRET, or "
        "GOOGLE_SEARCH_CONSOLE_ACCESS_TOKEN for a quick try."
    )
