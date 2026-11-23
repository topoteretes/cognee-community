"""Evernote OAuth 1.0a three-legged authentication helper.

Provides EvernoteOAuth for obtaining user access tokens via OAuth 1.0a.
"""

import os
from dataclasses import dataclass
from typing import Any
from urllib.parse import parse_qsl, quote

import httpx
from oauthlib.oauth1 import SIGNATURE_HMAC_SHA1, Client

from .evernote import EvernoteAuthError

PRODUCTION_HOST = "www.evernote.com"
SANDBOX_HOST = "sandbox.evernote.com"
# Confirmed against python-social-auth's Evernote backend; not tested against the live service.
OAUTH_REQUEST_TOKEN_PATH = "/oauth"
# Confirmed against python-social-auth's Evernote backend; not tested against the live service.
OAUTH_AUTHORIZE_PATH = "/OAuth.action"
# Confirmed against python-social-auth's Evernote backend; not tested against the live service.
OAUTH_ACCESS_TOKEN_PATH = "/oauth"


@dataclass
class EvernoteAccessToken:
    """Evernote access token and associated metadata from OAuth 1.0a handshake.

    Attributes:
        token: OAuth access token used to authenticate NoteStore calls.
        note_store_url: Base URL of the user's NoteStore service.
        user_id: Evernote EDAM numeric user identifier, or None if missing/invalid.
        expires: Expiration timestamp in milliseconds since the Unix epoch, or None.
    """

    token: str
    note_store_url: str
    user_id: int | None = None
    expires: int | None = None

    def __repr__(self) -> str:
        return (
            f"EvernoteAccessToken(token='***', note_store_url={self.note_store_url!r}, "
            f"user_id={self.user_id!r}, expires={self.expires!r})"
        )

    def __str__(self) -> str:
        return self.__repr__()


class EvernoteOAuth:
    """Helper for Evernote 3-legged OAuth 1.0a authentication flow."""

    def __init__(
        self,
        consumer_key: str | None = None,
        consumer_secret: str | None = None,
        sandbox: bool = False,
        http: Any = None,
    ) -> None:
        key = consumer_key or os.environ.get("EVERNOTE_CONSUMER_KEY")
        secret = consumer_secret or os.environ.get("EVERNOTE_CONSUMER_SECRET")
        if not key or not secret:
            raise ValueError(
                "Evernote consumer_key and consumer_secret are required: pass them or set "
                "EVERNOTE_CONSUMER_KEY and EVERNOTE_CONSUMER_SECRET environment variables."
            )
        self._consumer_key = key
        self._consumer_secret = secret
        self.sandbox = bool(sandbox)
        self.host = SANDBOX_HOST if self.sandbox else PRODUCTION_HOST
        self._http = http

    def __repr__(self) -> str:
        return f"EvernoteOAuth(consumer_key='***', sandbox={self.sandbox!r})"

    def __str__(self) -> str:
        return self.__repr__()

    def _send_request(
        self,
        url: str,
        headers: dict[str, str],
        body: str | bytes | None = None,
    ) -> str:
        try:
            if self._http is not None:
                resp = self._http.post(url, headers=headers, content=body)
            else:
                with httpx.Client(timeout=30.0) as client:
                    resp = client.post(url, headers=headers, content=body)

            status_code = getattr(resp, "status_code", 200)
            if status_code >= 400:
                raise EvernoteAuthError(f"OAuth request failed with HTTP status {status_code}")

            text = getattr(resp, "text", None)
            if text is None and hasattr(resp, "content"):
                text = resp.content.decode("utf-8", errors="replace")
            return text or ""
        except EvernoteAuthError:
            raise
        except httpx.HTTPError as exc:
            # Mask any internal details to prevent secret/token leakage
            raise EvernoteAuthError(f"OAuth request failed: {type(exc).__name__}") from None

    def get_request_token(self, callback_url: str) -> tuple[str, str]:
        """Obtain an unauthorized OAuth request token and token secret.

        Signs the request using OAuth 1.0a HMAC-SHA1.
        """
        if not callback_url:
            raise ValueError("callback_url is required")

        request_url = f"https://{self.host}{OAUTH_REQUEST_TOKEN_PATH}"
        client = Client(
            client_key=self._consumer_key,
            client_secret=self._consumer_secret,
            callback_uri=callback_url,
            signature_method=SIGNATURE_HMAC_SHA1,
        )
        # matches Evernote's JS client; not verified against the live service
        headers = {"Content-Type": "application/x-www-form-urlencoded"}
        _, headers, body = client.sign(request_url, http_method="POST", headers=headers, body="")

        response_text = self._send_request(request_url, headers=headers, body=body)

        params = dict(parse_qsl(response_text, keep_blank_values=True))
        oauth_token = params.get("oauth_token")
        oauth_token_secret = params.get("oauth_token_secret")

        if not oauth_token or not oauth_token_secret:
            raise EvernoteAuthError(
                "Invalid request token response: missing oauth_token or oauth_token_secret"
            )

        return oauth_token, oauth_token_secret

    def authorize_url(self, oauth_token: str) -> str:
        """Construct the user authorization URL for the given request token."""
        if not oauth_token:
            raise ValueError("oauth_token is required to construct authorize_url")
        return f"https://{self.host}{OAUTH_AUTHORIZE_PATH}?oauth_token={quote(oauth_token)}"

    def get_access_token(
        self,
        oauth_token: str,
        oauth_token_secret: str,
        verifier: str,
    ) -> EvernoteAccessToken:
        """Exchange authorized request token and verifier for an access token.

        Signs the request using OAuth 1.0a HMAC-SHA1.
        """
        if not oauth_token or not oauth_token_secret or not verifier:
            raise ValueError("oauth_token, oauth_token_secret, and verifier are required")

        access_url = f"https://{self.host}{OAUTH_ACCESS_TOKEN_PATH}"
        client = Client(
            client_key=self._consumer_key,
            client_secret=self._consumer_secret,
            resource_owner_key=oauth_token,
            resource_owner_secret=oauth_token_secret,
            verifier=verifier,
            signature_method=SIGNATURE_HMAC_SHA1,
        )
        # matches Evernote's JS client; not verified against the live service
        headers = {"Content-Type": "application/x-www-form-urlencoded"}
        _, headers, body = client.sign(access_url, http_method="POST", headers=headers, body="")

        response_text = self._send_request(access_url, headers=headers, body=body)

        params = dict(parse_qsl(response_text, keep_blank_values=True))
        token = params.get("oauth_token")
        if not token:
            raise EvernoteAuthError("Invalid access token response: missing oauth_token")

        note_store_url = params.get("edam_noteStoreUrl") or ""

        user_id_raw = params.get("edam_userId")
        user_id = None
        if user_id_raw is not None:
            try:
                user_id = int(user_id_raw)
            except (ValueError, TypeError):
                user_id = None

        expires_raw = params.get("edam_expires")
        expires = None
        if expires_raw is not None:
            try:
                expires = int(expires_raw)
            except (ValueError, TypeError):
                expires = None

        return EvernoteAccessToken(
            token=token,
            note_store_url=note_store_url,
            user_id=user_id,
            expires=expires,
        )
