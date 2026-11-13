"""DocuSign OAuth: JWT grant or a plain access token, plus account discovery.

Two ways to authenticate, both read as bearer tokens by the eSignature API:

* **JWT grant** (server setups): an integration key, the GUID of the user to
  impersonate and an RSA private key. The connector signs a short-lived
  assertion, exchanges it at ``/oauth/token`` and refreshes the token shortly
  before it expires. The user must have granted consent once (see README).
* **Access token** (quick tests, or a token obtained through the authorization
  code flow): used as is. It cannot be renewed, so a rejected token stops the
  sync with an error.

Secrets (private key, assertion, tokens) are never logged and never put in
error messages. Error messages carry DocuSign's error *code* only.
"""

import os
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import jwt
import requests

DEMO_OAUTH_HOST = "account-d.docusign.com"
PRODUCTION_OAUTH_HOST = "account.docusign.com"
OAUTH_HOSTS = {"demo": DEMO_OAUTH_HOST, "production": PRODUCTION_OAUTH_HOST}

# "impersonation" is what lets the integration act as the configured user.
JWT_SCOPE = "signature impersonation"
_JWT_LIFETIME_SECONDS = 3600
# Renew this long before the token expires so a long page walk never races it.
_REFRESH_MARGIN_SECONDS = 300
_REQUEST_TIMEOUT = 30

INTEGRATION_KEY_ENV = "DOCUSIGN_INTEGRATION_KEY"
USER_ID_ENV = "DOCUSIGN_USER_ID"
PRIVATE_KEY_ENV = "DOCUSIGN_PRIVATE_KEY"
PRIVATE_KEY_PATH_ENV = "DOCUSIGN_PRIVATE_KEY_PATH"
ACCESS_TOKEN_ENV = "DOCUSIGN_ACCESS_TOKEN"
ACCOUNT_ID_ENV = "DOCUSIGN_ACCOUNT_ID"
BASE_URI_ENV = "DOCUSIGN_BASE_URI"
ENVIRONMENT_ENV = "DOCUSIGN_ENVIRONMENT"


class DocuSignAuthError(RuntimeError):
    """Authentication or account discovery failed. Never carries a secret."""


@dataclass(frozen=True)
class DocuSignCredentials:
    """Resolved credentials: either a static ``access_token`` or the JWT triple."""

    environment: str
    # Secrets stay out of repr(), so they cannot leak through a log line or a traceback.
    access_token: str | None = field(default=None, repr=False)
    integration_key: str | None = None
    user_id: str | None = None
    private_key: str | None = field(default=None, repr=False)

    @property
    def oauth_host(self) -> str:
        return OAUTH_HOSTS[self.environment]


def resolve_credentials(
    integration_key: str | None = None,
    user_id: str | None = None,
    private_key: str | None = None,
    *,
    private_key_path: str | None = None,
    access_token: str | None = None,
    environment: str | None = None,
) -> DocuSignCredentials:
    """Merge arguments with the ``DOCUSIGN_*`` variables and validate the result.

    An ``access_token`` wins over the JWT triple when both are present, because
    passing a token explicitly is the more specific request.
    """
    environment = (environment or os.environ.get(ENVIRONMENT_ENV) or "demo").strip().lower()
    if environment not in OAUTH_HOSTS:
        raise ValueError(f"environment must be 'demo' or 'production', got {environment!r}.")

    access_token = access_token or os.environ.get(ACCESS_TOKEN_ENV)
    if access_token:
        return DocuSignCredentials(environment=environment, access_token=access_token)

    integration_key = integration_key or os.environ.get(INTEGRATION_KEY_ENV)
    user_id = user_id or os.environ.get(USER_ID_ENV)
    private_key = _load_private_key(private_key, private_key_path)
    missing = [
        name
        for name, value in (
            (INTEGRATION_KEY_ENV, integration_key),
            (USER_ID_ENV, user_id),
            (f"{PRIVATE_KEY_ENV} or {PRIVATE_KEY_PATH_ENV}", private_key),
        )
        if not value
    ]
    if missing:
        raise ValueError(
            "DocuSign credentials required: pass an access_token, or an integration_key, "
            f"user_id and private key, or set {ACCESS_TOKEN_ENV} / {', '.join(missing)}."
        )
    return DocuSignCredentials(
        environment=environment,
        integration_key=integration_key,
        user_id=user_id,
        private_key=private_key,
    )


def _load_private_key(private_key: str | None, private_key_path: str | None) -> str | None:
    """Return the PEM text from an argument, a file, or the environment."""
    path = private_key_path or os.environ.get(PRIVATE_KEY_PATH_ENV)
    if not private_key and path:
        with open(os.path.expanduser(path), encoding="utf-8") as handle:
            private_key = handle.read()
    private_key = private_key or os.environ.get(PRIVATE_KEY_ENV)
    if private_key:
        # A key exported through a single-line env var carries literal "\n".
        private_key = private_key.replace("\\n", "\n").strip()
    return private_key or None


class DocuSignAuth:
    """Hands out a valid bearer token, renewing a JWT-grant token before it expires."""

    def __init__(
        self,
        credentials: DocuSignCredentials,
        session: requests.Session | None = None,
        clock: Callable[[], float] = time.time,
    ):
        self._credentials = credentials
        self._session = session or requests.Session()
        self._clock = clock
        self._token = credentials.access_token
        self._expires_at = float("inf") if credentials.access_token else 0.0

    @property
    def renewable(self) -> bool:
        """Whether a rejected token can be replaced (JWT grant, not a static token)."""
        return self._credentials.access_token is None

    def token(self) -> str:
        if self._token is None or self._clock() >= self._expires_at - _REFRESH_MARGIN_SECONDS:
            self._token, self._expires_at = self._request_jwt_token()
        return self._token

    def invalidate(self) -> None:
        """Forget the cached token, so the next ``token()`` call fetches a new one."""
        if self.renewable:
            self._token = None

    def _request_jwt_token(self) -> tuple[str, float]:
        creds = self._credentials
        issued = int(self._clock())
        claims = {
            "iss": creds.integration_key,
            "sub": creds.user_id,
            "aud": creds.oauth_host,
            "iat": issued,
            "exp": issued + _JWT_LIFETIME_SECONDS,
            "scope": JWT_SCOPE,
        }
        try:
            assertion = jwt.encode(claims, creds.private_key, algorithm="RS256")
        except (ValueError, TypeError, jwt.PyJWTError):
            # Not chained on purpose: the original error can quote parts of the key.
            raise DocuSignAuthError(
                "The DocuSign private key could not be used to sign the JWT. "
                "Expected an RSA private key in PEM format."
            ) from None

        response = self._session.post(
            f"https://{creds.oauth_host}/oauth/token",
            data={
                "grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
                "assertion": assertion,
            },
            timeout=_REQUEST_TIMEOUT,
        )
        payload = _json_or_empty(response)
        if response.status_code != 200 or not payload.get("access_token"):
            code = payload.get("error") or f"HTTP {response.status_code}"
            raise DocuSignAuthError(_token_error_message(code, creds))
        expires_in = float(payload.get("expires_in") or _JWT_LIFETIME_SECONDS)
        return payload["access_token"], self._clock() + expires_in


def _token_error_message(code: str, creds: DocuSignCredentials) -> str:
    if code == "consent_required":
        return (
            "DocuSign has no consent for this integration key and user yet "
            "(consent_required). Open "
            f"https://{creds.oauth_host}/oauth/auth?response_type=code&scope="
            "signature%20impersonation&client_id=<integration key>&redirect_uri=<a redirect URI "
            "registered on the key> once as the user and click Accept, then retry."
        )
    if code == "invalid_grant":
        return (
            "DocuSign rejected the JWT (invalid_grant). Check the integration key, the user "
            "GUID, that the private key matches the RSA key pair on the integration key, and "
            f"that the environment ('{creds.environment}') matches where the key was created."
        )
    return f"DocuSign token request failed ({code})."


def resolve_account(
    session: requests.Session,
    auth: DocuSignAuth,
    oauth_host: str,
    account_id: str | None = None,
) -> tuple[str, str]:
    """Return ``(account_id, base_uri)`` for the account to read.

    Production accounts live on different hosts (na2, eu, ...), so the API base
    URL comes from ``/oauth/userinfo`` and is never hardcoded. Without an
    ``account_id`` the user's default account is used.
    """
    response = session.get(
        f"https://{oauth_host}/oauth/userinfo",
        headers={"Authorization": f"Bearer {auth.token()}"},
        timeout=_REQUEST_TIMEOUT,
    )
    if response.status_code != 200:
        raise DocuSignAuthError(
            f"DocuSign userinfo request failed (HTTP {response.status_code}); "
            "the token may be expired or issued by the other environment."
        )
    accounts: list[dict[str, Any]] = _json_or_empty(response).get("accounts") or []
    if account_id:
        match = next((a for a in accounts if a.get("account_id") == account_id), None)
        if match is None:
            raise DocuSignAuthError(
                f"Account {account_id} is not available to this user. "
                f"Available: {', '.join(a.get('account_id', '?') for a in accounts) or 'none'}."
            )
    else:
        match = next((a for a in accounts if _is_true(a.get("is_default"))), None) or (
            accounts[0] if accounts else None
        )
        if match is None:
            raise DocuSignAuthError("DocuSign userinfo listed no accounts for this user.")
    return match["account_id"], match["base_uri"].rstrip("/")


def _is_true(value: Any) -> bool:
    return value is True or str(value).lower() == "true"


def _json_or_empty(response: requests.Response) -> dict[str, Any]:
    try:
        payload = response.json()
    except ValueError:
        return {}
    return payload if isinstance(payload, dict) else {}
