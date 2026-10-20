"""Read-only HTTP client for the DocuSign eSignature REST API v2.1.

Only ``GET`` requests are ever issued. The client owns the three things the
sync depends on for safety:

* **Token renewal**: a ``401`` renews a JWT-grant token once and retries.
* **Rate limits**: a ``429`` waits for ``Retry-After`` / ``X-RateLimit-Reset``
  when that is short, and otherwise stops with :class:`DocuSignRateLimitError`
  instead of sleeping for the rest of the hour.
* **Failure is loud**: anything else that is not a success raises, so a failed
  page aborts the run before any forget-on-delete decision is made.

Error messages carry the HTTP status and DocuSign's ``errorCode`` only; request
headers (the bearer token) never reach a message.
"""

import time
from collections.abc import Callable, Iterator
from typing import Any
from urllib.parse import urlsplit

import requests
from cognee.shared.logging_utils import get_logger

from cognee_community_connector_docusign.auth import DocuSignAuth, DocuSignAuthError

logger = get_logger("docusign_connector")

_REQUEST_TIMEOUT = 60
_MAX_ATTEMPTS = 5
# A rate-limit wait longer than this aborts the run: the next sync retries.
_MAX_RATE_LIMIT_WAIT = 120.0
_MAX_BACKOFF = 30.0


class DocuSignAPIError(RuntimeError):
    """The eSignature API answered with an error (or kept failing)."""

    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


class DocuSignRateLimitError(DocuSignAPIError):
    """DocuSign asked for a wait longer than the connector is willing to sleep."""


class DocuSignClient:
    """Authenticated ``GET`` access to one account's eSignature API."""

    def __init__(
        self,
        auth: DocuSignAuth,
        base_uri: str,
        account_id: str,
        session: requests.Session | None = None,
        *,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.time,
    ):
        self._auth = auth
        self._session = session or requests.Session()
        self._sleep = sleep
        self._clock = clock
        self.account_id = account_id
        self._host_root = base_uri.rstrip("/")
        self._api_root = f"{self._host_root}/restapi/v2.1/accounts/{account_id}"

    def get_json(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        """``GET`` an account-relative path and return the JSON object (``{}`` if empty)."""
        response = self._get(self._url(path), params, accept="application/json")
        if not response.content:
            return {}
        try:
            payload = response.json()
        except ValueError as exc:
            raise DocuSignAPIError(f"DocuSign returned invalid JSON for {path}.") from exc
        return payload if isinstance(payload, dict) else {}

    def get_bytes(self, path: str, params: dict[str, Any] | None = None) -> bytes:
        """``GET`` an account-relative path and return the raw body (a PDF)."""
        return self._get(self._url(path), params, accept="application/pdf").content

    def paginate_envelopes(self, params: dict[str, Any]) -> Iterator[dict[str, Any]]:
        """Yield every envelope of a ``/envelopes`` listing, following ``nextUri``."""
        path: str = "envelopes"
        query: dict[str, Any] | None = params
        while True:
            page = self.get_json(path, query)
            yield from page.get("envelopes") or []
            next_uri = page.get("nextUri")
            if not next_uri:
                return
            # nextUri already carries every query parameter of the first request.
            path, query = next_uri, None

    # ------------------------------------------------------------------

    def _url(self, path: str) -> str:
        if path.startswith(("http://", "https://")):
            # Keep the path and query but never the host: the bearer token is
            # only ever sent to the account's own API host.
            parts = urlsplit(path)
            path = parts.path + (f"?{parts.query}" if parts.query else "")
        if path.startswith("/restapi/"):
            return f"{self._host_root}{path}"
        return f"{self._api_root}/{path.lstrip('/')}"

    def _get(self, url: str, params: dict[str, Any] | None, accept: str) -> requests.Response:
        renewed = False
        for attempt in range(1, _MAX_ATTEMPTS + 1):
            last = attempt == _MAX_ATTEMPTS
            try:
                response = self._session.get(
                    url,
                    params=params,
                    headers={"Authorization": f"Bearer {self._auth.token()}", "Accept": accept},
                    timeout=_REQUEST_TIMEOUT,
                )
            except requests.RequestException as exc:
                if last:
                    raise DocuSignAPIError(
                        f"DocuSign request failed: {type(exc).__name__}."
                    ) from None
                self._sleep(_backoff(attempt))
                continue

            status = response.status_code
            if status == 401 and not renewed and self._auth.renewable:
                renewed = True
                self._auth.invalidate()
                continue
            if status == 401:
                raise DocuSignAuthError(
                    "DocuSign rejected the access token (HTTP 401). A static access token "
                    "expires after about an hour; use the JWT grant for unattended syncs."
                )
            if status == 429:
                wait = self._rate_limit_wait(response, attempt)
                if wait > _MAX_RATE_LIMIT_WAIT or last:
                    raise DocuSignRateLimitError(
                        f"DocuSign rate limit reached (HTTP 429); retry in about {int(wait)}s. "
                        "Nothing was changed; the next sync resumes from the same point.",
                        status,
                    )
                logger.warning("DocuSign: rate limited, waiting %.0fs.", wait)
                self._sleep(wait)
                continue
            if status >= 500 and not last:
                self._sleep(_backoff(attempt))
                continue
            if status >= 400:
                raise DocuSignAPIError(_error_message(response), status)
            return response
        raise AssertionError("unreachable")  # pragma: no cover

    def _rate_limit_wait(self, response: requests.Response, attempt: int) -> float:
        headers = response.headers
        retry_after = _number(headers.get("Retry-After"))
        if retry_after is not None:
            return retry_after
        reset = _number(headers.get("X-RateLimit-Reset"))
        if reset is not None:
            # DocuSign sends an epoch timestamp for when the window reopens.
            return max(reset - self._clock(), 1.0)
        return _backoff(attempt)


def _backoff(attempt: int) -> float:
    return min(2.0 ** (attempt - 1), _MAX_BACKOFF)


def _number(value: str | None) -> float | None:
    try:
        return float(value) if value is not None else None
    except ValueError:
        return None


def _error_message(response: requests.Response) -> str:
    """``HTTP <status> <errorCode>`` plus DocuSign's own message, never the request."""
    code = message = ""
    try:
        payload = response.json()
    except ValueError:
        payload = None
    if isinstance(payload, dict):
        code = str(payload.get("errorCode") or payload.get("error") or "")
        message = str(payload.get("message") or payload.get("error_description") or "")
    detail = " ".join(part for part in (code, message) if part)
    return f"DocuSign API error: HTTP {response.status_code}" + (f" {detail}" if detail else "")
