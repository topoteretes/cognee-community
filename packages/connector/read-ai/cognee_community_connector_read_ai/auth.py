"""Read.ai OAuth credentials and verified OpenID Connect account binding."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import math
import os
import stat
import tempfile
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

import httpx
import jwt
from filelock import FileLock

from .models import AccountMismatchError, AuthenticationError

ISSUER = "https://authn.read.ai/"
JWKS_URL = "https://authn.read.ai/.well-known/jwks.json"
USERINFO_URL = "https://authn.read.ai/userinfo"
TOKEN_URL = "https://authn.read.ai/oauth2/token"
REGISTER_URL = "https://api.read.ai/oauth/register"
REDIRECT_URI = "https://api.read.ai/oauth/ui"
AUTH_TIMEOUT = 30.0
MAX_AUTH_RESPONSE_BYTES = 1024 * 1024


@dataclass(frozen=True)
class OAuthIdentity:
    issuer: str
    subject: str


@dataclass(frozen=True)
class TokenBundle:
    access_token: str = field(repr=False)
    client_id: str
    id_token: str | None = field(default=None, repr=False)
    nonce: str | None = field(default=None, repr=False)


class TokenProvider(Protocol):
    def get_tokens(self, *, force_refresh: bool = False) -> TokenBundle: ...


def _text(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise AuthenticationError(f"Missing or invalid OAuth {name}; reconnect Read.ai.")
    return value


def _json_request(client: httpx.Client, method: str, url: str, **kwargs: Any) -> dict:
    try:
        with client.stream(
            method, url, follow_redirects=False, timeout=AUTH_TIMEOUT, **kwargs
        ) as response:
            if response.status_code >= 500:
                raise _TransientOAuthError()
            if not 200 <= response.status_code < 300:
                raise AuthenticationError(
                    f"OAuth endpoint returned HTTP {response.status_code}; reconnect Read.ai."
                )
            data = bytearray()
            for chunk in response.iter_bytes():
                data.extend(chunk)
                if len(data) > MAX_AUTH_RESPONSE_BYTES:
                    raise AuthenticationError("OAuth response exceeded the size limit.")
        value = json.loads(data)
        if not isinstance(value, dict):
            raise AuthenticationError("OAuth endpoint returned an invalid response.")
        return value
    except (httpx.TimeoutException, httpx.TransportError):
        raise _TransientOAuthError() from None
    except (ValueError, UnicodeError):
        raise AuthenticationError("OAuth endpoint returned invalid JSON.") from None


class _TransientOAuthError(AuthenticationError):
    def __init__(self) -> None:
        super().__init__("OAuth request failed transiently; retry or reconnect Read.ai.")


def _verified_claims(bundle: TokenBundle, request_json: Callable[..., dict]) -> dict:
    token = _text(bundle.id_token, "ID token")
    try:
        header = jwt.get_unverified_header(token)
        if header.get("alg") != "RS256" or not isinstance(header.get("kid"), str):
            raise AuthenticationError("Unsupported ID token signing key or algorithm.")
        # The token may select a key, but never a key URL, issuer, or algorithm.
        keys = request_json(JWKS_URL).get("keys")
        if not isinstance(keys, list):
            raise AuthenticationError("Read.ai returned an invalid signing-key set.")
        matches = [
            key
            for key in keys
            if isinstance(key, dict)
            and key.get("kid") == header["kid"]
            and key.get("kty") == "RSA"
            and key.get("use", "sig") == "sig"
            and key.get("alg", "RS256") == "RS256"
            and "verify" in key.get("key_ops", ["verify"])
        ]
        if len(matches) != 1:
            raise AuthenticationError("ID token signing key is unavailable or ambiguous.")
        key = jwt.PyJWK.from_dict(matches[0], algorithm="RS256").key
        claims = jwt.decode(
            token,
            key,
            algorithms=["RS256"],
            audience=_text(bundle.client_id, "client ID"),
            issuer=ISSUER,
            options={"require": ["exp", "iat", "iss", "sub", "aud"]},
        )
        _text(claims.get("sub"), "subject")
        # Numeric dates must be finite JSON numbers, not bools or numeric strings.
        for name in ("iat", "exp"):
            value = claims[name]
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise AuthenticationError("Invalid ID token timestamp.")
            if not math.isfinite(value):
                raise AuthenticationError("Invalid ID token timestamp.")
        audience = claims["aud"]
        if isinstance(audience, list) and len(audience) > 1:
            if claims.get("azp") != bundle.client_id:
                raise AuthenticationError("ID token authorized party does not match this client.")
        if "azp" in claims and claims["azp"] != bundle.client_id:
            raise AuthenticationError("ID token authorized party does not match this client.")
        if bundle.nonce is not None:
            if not isinstance(claims.get("nonce"), str) or not hmac.compare_digest(
                claims["nonce"], bundle.nonce
            ):
                raise AuthenticationError(
                    "ID token nonce does not match the authorization request."
                )
        if "at_hash" in claims:
            digest = hashlib.sha256(bundle.access_token.encode("ascii")).digest()
            expected_hash = base64.urlsafe_b64encode(digest[:16]).rstrip(b"=").decode("ascii")
            if not isinstance(claims["at_hash"], str) or not hmac.compare_digest(
                claims["at_hash"], expected_hash
            ):
                raise AuthenticationError("ID token access-token hash does not match.")
        return claims
    except (jwt.PyJWTError, ValueError, TypeError, KeyError, UnicodeError, OverflowError):
        raise AuthenticationError(
            "Read.ai ID token verification failed; reconnect Read.ai."
        ) from None


def verify_identity(
    bundle: TokenBundle,
    *,
    client: httpx.Client,
    expected: OAuthIdentity | None = None,
    request_json: Callable[..., dict] | None = None,
) -> OAuthIdentity:
    """Bind to signed identity initially; recheck authenticated UserInfo each run."""
    _text(bundle.access_token, "access token")
    _text(bundle.client_id, "client ID")
    if expected is not None and expected.issuer != ISSUER:
        raise AccountMismatchError("Stored OAuth issuer does not match Read.ai.")
    get_json = request_json or (lambda url, **kwargs: _json_request(client, "GET", url, **kwargs))
    claims = _verified_claims(bundle, get_json) if bundle.id_token is not None else None
    if expected is None and claims is None:
        raise AuthenticationError("An ID token is required to establish the Read.ai account.")
    identity = OAuthIdentity(ISSUER, claims["sub"]) if claims else expected
    if expected is not None and identity != expected:
        raise AccountMismatchError("Read.ai account changed; use a new connection ID.")
    userinfo = get_json(USERINFO_URL, headers={"Authorization": f"Bearer {bundle.access_token}"})
    subject = _text(userinfo.get("sub"), "UserInfo subject")
    if identity is None or subject != identity.subject:
        raise AccountMismatchError("Read.ai UserInfo does not match the bound account.")
    return identity


def save_credentials(path: Path | str, values: dict[str, Any]) -> None:
    """Persist credentials durably and atomically before returning rotated tokens."""
    path = Path(path).expanduser()
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if path.is_symlink():
        raise AuthenticationError("Refusing to write credentials through a symbolic link.")
    temp_path: str | None = None
    try:
        fd, temp_path = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            os.fchmod(handle.fileno(), 0o600)
            json.dump(values, handle, allow_nan=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, path)
        temp_path = None
        directory_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    except (OSError, ValueError, TypeError):
        raise AuthenticationError(
            "Could not durably save OAuth credentials; reconnect Read.ai."
        ) from None
    finally:
        if temp_path is not None:
            os.unlink(temp_path)


def _load_credentials(path: Path) -> dict:
    try:
        info = path.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077:
            raise AuthenticationError(
                "OAuth credential file must be a regular file with mode 0600."
            )
        if info.st_size > MAX_AUTH_RESPONSE_BYTES:
            raise AuthenticationError("OAuth credential file exceeded the size limit.")
        value = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(value, dict):
            raise ValueError()
        return value
    except (OSError, ValueError, UnicodeError):
        raise AuthenticationError("Cannot read OAuth credentials; run read-ai-connect.") from None


def _token_values(response: dict, previous: dict) -> dict:
    token_type = response.get("token_type")
    if not isinstance(token_type, str) or token_type.lower() != "bearer":
        raise AuthenticationError("OAuth response did not issue a bearer token.")
    expiry = response.get("expires_in")
    if isinstance(expiry, bool) or not isinstance(expiry, (int, float)):
        raise AuthenticationError("OAuth response has invalid token expiry.")
    if not math.isfinite(expiry) or expiry <= 0:
        raise AuthenticationError("OAuth response has invalid token expiry.")
    values = dict(previous)
    values.update(
        access_token=_text(response.get("access_token"), "access token"),
        refresh_token=_text(response.get("refresh_token"), "rotated refresh token"),
        expires_at=time.time() + expiry,
    )
    # An expired initial ID token must never accompany a refreshed access token.
    values.pop("id_token", None)
    values.pop("nonce", None)
    if response.get("id_token") is not None:
        values["id_token"] = _text(response["id_token"], "ID token")
    return values


class FileTokenProvider:
    """Shared token store with serialized, rotating refresh-token exchanges."""

    def __init__(self, path: Path | str, *, client: httpx.Client | None = None) -> None:
        self.path = Path(path).expanduser()
        self._client = client

    def __repr__(self) -> str:
        return f"FileTokenProvider(path={self.path!r})"

    def get_tokens(self, *, force_refresh: bool = False) -> TokenBundle:
        observed = _load_credentials(self.path)
        with FileLock(str(self.path) + ".lock", timeout=60, mode=0o600):
            values = _load_credentials(self.path)
            client_id = _text(values.get("client_id"), "client ID")
            access_token = _text(values.get("access_token"), "access token")
            expiry = values.get("expires_at")
            if isinstance(expiry, bool) or not isinstance(expiry, (int, float)):
                raise AuthenticationError(
                    "OAuth credentials have invalid expiry; reconnect Read.ai."
                )
            fresh = math.isfinite(expiry) and expiry > time.time() + 60
            already_refreshed = observed.get("access_token") != access_token
            if not fresh or (force_refresh and not already_refreshed):
                secret = _text(values.get("client_secret"), "client secret")
                refresh = _text(values.get("refresh_token"), "refresh token")
                client = self._client or httpx.Client()
                try:
                    for attempt in range(2):
                        try:
                            response = _json_request(
                                client,
                                "POST",
                                TOKEN_URL,
                                auth=httpx.BasicAuth(client_id, secret),
                                data={"grant_type": "refresh_token", "refresh_token": refresh},
                            )
                            break
                        except _TransientOAuthError:
                            if attempt:
                                raise AuthenticationError(
                                    "OAuth refresh recovery failed; reconnect Read.ai."
                                ) from None
                    values = _token_values(response, values)
                    save_credentials(self.path, values)
                finally:
                    if self._client is None:
                        client.close()
            return TokenBundle(
                _text(values.get("access_token"), "access token"),
                client_id,
                values.get("id_token"),
                values.get("nonce"),
            )


def register_client(client: httpx.Client, client_name: str) -> dict:
    values = _json_request(
        client,
        "POST",
        REGISTER_URL,
        json={
            "client_name": _text(client_name, "client name"),
            "redirect_uris": [REDIRECT_URI],
            "grant_types": ["authorization_code", "refresh_token"],
            "response_types": ["code"],
            "scope": "openid email offline_access profile meeting:read mcp:execute",
            "token_endpoint_auth_method": "client_secret_basic",
        },
    )
    return {
        "client_id": _text(values.get("client_id"), "client ID"),
        "client_secret": _text(values.get("client_secret"), "client secret"),
    }


def exchange_code(
    client: httpx.Client,
    *,
    client_id: str,
    client_secret: str,
    code: str,
    code_verifier: str,
) -> dict:
    """Exchange values from the hosted UI directly; never execute pasted commands."""
    response = _json_request(
        client,
        "POST",
        TOKEN_URL,
        auth=httpx.BasicAuth(_text(client_id, "client ID"), _text(client_secret, "client secret")),
        data={
            "grant_type": "authorization_code",
            "code": _text(code, "authorization code"),
            "redirect_uri": REDIRECT_URI,
            "code_verifier": _text(code_verifier, "PKCE verifier"),
        },
    )
    return _token_values(response, {"client_id": client_id, "client_secret": client_secret})


def default_credentials_path() -> Path:
    base = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    return base / "cognee-read-ai" / "credentials.json"
