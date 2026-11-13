"""OAuth 2.0 authorization-code flow for Salesloft.

Handles:
- Authorization URL generation
- Code exchange for tokens
- Automatic refresh with rotation (Salesloft rotates refresh tokens)
- Pluggable token storage (env, file, or callback)
- Fail-fast /v2/me check
"""

from __future__ import annotations

import contextlib
import json
import os
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Protocol
from urllib.parse import urlencode

import httpx
from cognee.shared.logging_utils import get_logger

logger = get_logger("salesloft_auth")

SALESLOFT_AUTH_URL = "https://accounts.salesloft.com/oauth/authorize"
SALESLOFT_TOKEN_URL = "https://accounts.salesloft.com/oauth/token"
SALESLOFT_API_BASE = "https://api.salesloft.com"

# Default redirect for local dev / scripts.
DEFAULT_REDIRECT_URI = "http://localhost:8080/callback"


class TokenStore(Protocol):
    """Interface for persisting OAuth tokens."""

    def load(self) -> dict[str, Any] | None:
        """Load stored tokens, or None if not yet persisted."""
        ...

    def save(self, tokens: dict[str, Any]) -> None:
        """Persist tokens (must be called before using the access token)."""
        ...


@dataclass
class EnvTokenStore:
    """Read tokens from environment variables (read-only, for CI/scripts)."""

    access_token_var: str = "SALESLOFT_ACCESS_TOKEN"
    refresh_token_var: str = "SALESLOFT_REFRESH_TOKEN"

    def load(self) -> dict[str, Any] | None:
        access = os.environ.get(self.access_token_var)
        if not access:
            return None
        return {
            "access_token": access,
            "refresh_token": os.environ.get(self.refresh_token_var, ""),
            "expires_at": 0,  # always attempt refresh
        }

    def save(self, tokens: dict[str, Any]) -> None:
        # Environment-based store is read-only; log a warning if rotation happens.
        logger.warning("EnvTokenStore is read-only. New tokens must be set manually in env vars.")


@dataclass
class FileTokenStore:
    """Persist tokens in a local JSON file (0600 permissions)."""

    path: str = "salesloft_tokens.json"

    def load(self) -> dict[str, Any] | None:
        if not os.path.exists(self.path):
            return None
        with open(self.path, encoding="utf-8") as f:
            return json.load(f)

    def save(self, tokens: dict[str, Any]) -> None:
        fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(tokens, f, indent=2)
        with contextlib.suppress(OSError):  # Windows may not support chmod
            os.chmod(self.path, 0o600)


@dataclass
class CallbackTokenStore:
    """Delegate token persistence to user-supplied callbacks."""

    load_fn: Callable[[], dict[str, Any] | None]
    save_fn: Callable[[dict[str, Any]], None]

    def load(self) -> dict[str, Any] | None:
        return self.load_fn()

    def save(self, tokens: dict[str, Any]) -> None:
        self.save_fn(tokens)


def authorization_url(
    client_id: str,
    redirect_uri: str = DEFAULT_REDIRECT_URI,
) -> str:
    """Generate the Salesloft OAuth authorization URL."""
    params = {
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
    }
    return f"{SALESLOFT_AUTH_URL}?{urlencode(params)}"


def exchange_code(
    code: str,
    client_id: str,
    client_secret: str,
    redirect_uri: str = DEFAULT_REDIRECT_URI,
    token_store: TokenStore | None = None,
) -> dict[str, Any]:
    """Exchange an authorization code for access + refresh tokens.

    Salesloft rotates refresh tokens on every exchange, so the new refresh
    token is persisted BEFORE the access token is used.
    """
    with httpx.Client() as http:
        response = http.post(
            SALESLOFT_TOKEN_URL,
            data={
                "grant_type": "authorization_code",
                "code": code,
                "client_id": client_id,
                "client_secret": client_secret,
                "redirect_uri": redirect_uri,
            },
        )
        response.raise_for_status()
        tokens = response.json()

    result = {
        "access_token": tokens["access_token"],
        "refresh_token": tokens["refresh_token"],
        "expires_at": time.time() + tokens.get("expires_in", 7200),
    }

    # Persist BEFORE using — Salesloft rotates refresh tokens.
    if token_store:
        token_store.save(result)

    return result


def refresh_tokens(
    refresh_token: str,
    client_id: str,
    client_secret: str,
    token_store: TokenStore | None = None,
) -> dict[str, Any]:
    """Refresh an expired access token. Persists the rotated refresh token first."""
    with httpx.Client() as http:
        response = http.post(
            SALESLOFT_TOKEN_URL,
            data={
                "grant_type": "refresh_token",
                "refresh_token": refresh_token,
                "client_id": client_id,
                "client_secret": client_secret,
            },
        )
        response.raise_for_status()
        tokens = response.json()

    result = {
        "access_token": tokens["access_token"],
        "refresh_token": tokens["refresh_token"],
        "expires_at": time.time() + tokens.get("expires_in", 7200),
    }

    # Persist rotated refresh token BEFORE using the new access token.
    if token_store:
        token_store.save(result)

    return result


def get_valid_access_token(
    client_id: str,
    client_secret: str,
    token_store: TokenStore,
) -> str:
    """Return a valid access token, refreshing if expired.

    Raises ValueError if no tokens are stored and no code is available.
    """
    stored = token_store.load()
    if not stored:
        raise ValueError(
            "No Salesloft tokens found. Run the OAuth flow first via "
            "exchange_code() or set SALESLOFT_ACCESS_TOKEN."
        )

    # Refresh if expired (with 60s buffer).
    if stored.get("expires_at", 0) < time.time() + 60:
        rt = stored.get("refresh_token")
        if not rt:
            raise ValueError(
                "Access token expired and no refresh token available. Re-run the OAuth flow."
            )
        stored = refresh_tokens(rt, client_id, client_secret, token_store)

    return stored["access_token"]


def verify_credentials(access_token: str) -> dict[str, Any]:
    """Fail-fast check: call /v2/me and return the user profile.

    Raises httpx.HTTPStatusError with an actionable message on failure.
    Credentials are never logged.
    """
    with httpx.Client() as http:
        response = http.get(
            f"{SALESLOFT_API_BASE}/v2/me",
            headers={"Authorization": f"Bearer {access_token}"},
        )
    if response.status_code == 401:
        raise ValueError(
            "Salesloft: invalid or expired access token. Re-run the OAuth flow or check your token."
        )
    if response.status_code == 403:
        raise ValueError("Salesloft: insufficient scopes. Ensure the OAuth app has read access.")
    response.raise_for_status()
    return response.json().get("data", {})
