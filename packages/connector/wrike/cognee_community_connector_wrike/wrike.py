"""Wrike connector for cognee: a ``dlt`` source that syncs Wrike folders and tasks into memory.

Auth
----
Two ways in, both ending in the same ``Authorization: bearer <token>`` header:

* **Permanent token**: ``access_token`` (or ``WRIKE_ACCESS_TOKEN``), used as is.
* **OAuth 2.0**: ``client_id`` + ``client_secret`` + ``token_path``. Each run
  exchanges the refresh token in ``token_path`` for a fresh access token.
  Wrike rotates the refresh token on every refresh and the old one stops
  working at once (tested), so the new token response is written back to
  ``token_path`` before any API call. The write goes to a temp file that is
  renamed over the old one, so a crash can never leave a half-written file.
  The browser consent that produces the first refresh token is a one-time
  step, documented in the README; this module never opens a browser.

Wrike keeps each account in one data centre (``www.wrike.com``,
``app-eu.wrike.com``, ...). The OAuth token response names it in ``host``; a
permanent-token user sets ``host`` / ``WRIKE_HOST``.
"""

import json
import os
import tempfile
import time
from pathlib import Path
from typing import Any

import httpx
from cognee.shared.logging_utils import get_logger

logger = get_logger("wrike_connector")

DEFAULT_HOST = "www.wrike.com"
TOKEN_URL = "https://login.wrike.com/oauth2/token"
# Wrike allows pageSize 1..1000 on list endpoints (1001 is rejected).
PAGE_SIZE = 1000

_MAX_RETRIES = 5
_MAX_RETRY_DELAY = 60.0
_RETRY_STATUSES = (429, 500, 502, 503, 504)
_TIMEOUT = 30.0


class WrikeAPIError(RuntimeError):
    """A Wrike request failed. The message carries the status and Wrike's error code only."""

    def __init__(self, message: str, status: int | None = None, code: str | None = None):
        super().__init__(message)
        self.status = status
        self.code = code


class WrikeAuthError(WrikeAPIError):
    """The token was rejected, or the OAuth refresh failed."""


def _check_token(token: str, name: str) -> str:
    # Refuse a malformed secret before it is ever sent, without echoing it.
    token = (token or "").strip()
    if not token or not token.isascii() or any(c.isspace() for c in token):
        raise ValueError(f"{name} is empty or malformed.")
    return token


def _check_host(host: str) -> str:
    # The token is sent to this host, so only accept Wrike's own data centres.
    host = (host or "").strip().lower()
    if host != "wrike.com" and not host.endswith(".wrike.com"):
        raise ValueError(f"Wrike host must be a wrike.com host, got {host!r}.")
    return host


def _encode(params: dict[str, Any]) -> dict[str, str]:
    # Wrike takes lists and objects (fields, updatedDate, ...) as JSON strings.
    return {k: v if isinstance(v, str) else json.dumps(v) for k, v in params.items()}


def _retry_delay(response: httpx.Response | None, attempt: int) -> float:
    header = response.headers.get("retry-after") if response is not None else None
    try:
        delay = float(header)
    except (TypeError, ValueError):
        delay = 2.0**attempt
    return min(delay, _MAX_RETRY_DELAY)


def _error_from(response: httpx.Response) -> WrikeAPIError:
    try:
        code = response.json().get("error")
    except ValueError:
        code = None
    cls = WrikeAuthError if response.status_code == 401 else WrikeAPIError
    hint = " Check the access token, or redo the OAuth consent." if cls is WrikeAuthError else ""
    return cls(
        f"Wrike API request failed: HTTP {response.status_code} ({code or 'no error code'}).{hint}",
        status=response.status_code,
        code=code,
    )


class WrikeClient:
    """Minimal Wrike API v4 client: auth header, retries, and ``nextPageToken`` paging."""

    def __init__(
        self,
        access_token: str,
        host: str = DEFAULT_HOST,
        http: httpx.Client | None = None,
    ):
        self._base = f"https://{_check_host(host)}/api/v4"
        self._http = http or httpx.Client(timeout=_TIMEOUT)
        self._headers = {"Authorization": f"bearer {_check_token(access_token, 'access_token')}"}

    def _request(self, path: str, params: dict[str, Any]) -> dict:
        """GET one page, retrying rate limits (429), 5xx and network errors."""
        for attempt in range(_MAX_RETRIES):
            response = None
            try:
                response = self._http.get(
                    self._base + path, params=_encode(params), headers=self._headers
                )
            except httpx.TransportError as exc:
                if attempt == _MAX_RETRIES - 1:
                    raise WrikeAPIError(f"Wrike API unreachable: {type(exc).__name__}.") from exc
            else:
                if response.status_code == 200:
                    return response.json()
                if response.status_code not in _RETRY_STATUSES or attempt == _MAX_RETRIES - 1:
                    raise _error_from(response)
            delay = _retry_delay(response, attempt)
            logger.warning(
                "Wrike: retrying %s in %.1fs (%d/%d).", path, delay, attempt + 1, _MAX_RETRIES
            )
            time.sleep(delay)
        raise AssertionError("unreachable")

    def get(self, path: str, **params: Any) -> list[dict]:
        """GET ``path`` and return ``data`` from every page.

        Paging only happens when ``pageSize`` is passed (list endpoints); the
        original params are repeated on each page together with the token.
        """
        items: list[dict] = []
        while True:
            body = self._request(path, params)
            items.extend(body.get("data", []))
            token = body.get("nextPageToken")
            if not token:
                return items
            if token == params.get("nextPageToken"):
                # A token that does not advance would loop forever.
                raise WrikeAPIError(f"Wrike returned the same nextPageToken twice for {path}.")
            params = {**params, "nextPageToken": token}


def refresh_access_token(
    client_id: str,
    client_secret: str,
    token_path: str | os.PathLike,
    http: httpx.Client | None = None,
) -> tuple[str, str]:
    """Trade the refresh token in ``token_path`` for a new access token.

    Returns ``(access_token, host)``. The rotated token response is saved back
    to ``token_path`` before returning; on any failure the file is untouched.
    """
    path = Path(token_path).expanduser()
    try:
        refresh_token = json.loads(path.read_text())["refresh_token"]
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise WrikeAuthError(
            f"Could not read a refresh_token from {path} ({type(exc).__name__})."
        ) from exc

    http = http or httpx.Client(timeout=_TIMEOUT)
    try:
        response = http.post(
            TOKEN_URL,
            data={
                "client_id": _check_token(client_id, "client_id"),
                "client_secret": _check_token(client_secret, "client_secret"),
                "grant_type": "refresh_token",
                "refresh_token": _check_token(refresh_token, "refresh_token"),
            },
        )
    except httpx.TransportError as exc:
        raise WrikeAuthError(f"Wrike OAuth refresh failed: {type(exc).__name__}.") from exc

    try:
        body = response.json()
    except ValueError:
        body = {}
    if response.status_code != 200 or "access_token" not in body:
        raise WrikeAuthError(
            f"Wrike OAuth refresh failed: HTTP {response.status_code} "
            f"({body.get('error') or 'no error code'}). The refresh token may be used up; "
            "redo the OAuth consent described in the README.",
            status=response.status_code,
            code=body.get("error"),
        )

    host = _check_host(body.get("host") or DEFAULT_HOST)
    _write_private_json(path, body)
    return body["access_token"], host


def _write_private_json(path: Path, data: dict) -> None:
    """Atomically replace ``path`` with ``data``, readable by the owner only."""
    # mkstemp creates the file with mode 0600, and the rename keeps that mode.
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(data, f)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
