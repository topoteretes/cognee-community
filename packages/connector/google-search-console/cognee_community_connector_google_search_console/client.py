"""A small client for the Search Console API: bearer auth, retries, ``startRow`` pagination."""

import time
from collections.abc import Callable
from typing import Any
from urllib.parse import quote

import httpx
from cognee.shared.logging_utils import get_logger

from .auth import SearchConsoleCredentials

logger = get_logger("google_search_console_connector")

API_URL = "https://searchconsole.googleapis.com/webmasters/v3"

# The most rows one searchAnalytics.query call returns.
ROW_LIMIT = 25_000
# Retry budget for rate limits (429), server errors and network failures.
_MAX_RETRIES = 5
_RETRY_STATUS = {429, 500, 502, 503, 504}
# Google reports a per-minute quota as 403 with one of these reasons.
_RATE_LIMIT_REASONS = {"rateLimitExceeded", "userRateLimitExceeded"}
# One wait is capped, and so is all waiting of one client: a sync holds cognee's process-wide
# dlt staging lock, so it must give up and resume next run instead of sleeping for minutes.
_MAX_WAIT = 60.0
_SLEEP_BUDGET = 300.0


class SearchConsoleAPIError(Exception):
    """A Search Console API call failed for good (not a retried rate limit or blip)."""

    def __init__(self, status: int, message: str, reason: str | None = None):
        super().__init__(
            f"Search Console API error {status}{f' ({reason})' if reason else ''}: {message}"
        )
        self.status = status
        self.reason = reason
        self.message = message

    @property
    def is_rate_limit(self) -> bool:
        """Google is throttling us (or asks for a wait too long to sit through)."""
        return self.status == 429 or self.reason in _RATE_LIMIT_REASONS


class SearchConsoleClient:
    """Calls the Search Console API as the signed-in user.

    Args:
        credentials: Where the bearer token comes from; renewed once on a 401.
        base_url: API root; override for a proxy.
        transport: An ``httpx`` transport, mainly a test-injection point.
        sleep: Called to wait out rate limits and backoff.
    """

    def __init__(
        self,
        credentials: SearchConsoleCredentials,
        *,
        base_url: str = API_URL,
        transport: httpx.BaseTransport | None = None,
        sleep: Callable[[float], None] = time.sleep,
        timeout: float = 60.0,
    ):
        self._credentials = credentials
        self._sleep = sleep
        self._slept = 0.0
        self._http = httpx.Client(
            base_url=base_url.rstrip("/"),
            headers={"Accept": "application/json"},
            transport=transport,
            timeout=timeout,
        )

    def close(self) -> None:
        self._http.close()

    def list_sites(self) -> list[dict]:
        """The properties this account can see, each ``{siteUrl, permissionLevel}``."""
        return self._request("GET", "/sites").get("siteEntry") or []

    def get_site(self, site_url: str) -> dict:
        return self._request("GET", f"/sites/{_encode(site_url)}")

    def query(self, site_url: str, body: dict[str, Any]) -> list[dict]:
        """One ``searchAnalytics.query`` call; its rows, sorted by clicks."""
        path = f"/sites/{_encode(site_url)}/searchAnalytics/query"
        return self._request("POST", path, body).get("rows") or []

    def query_all(self, site_url: str, body: dict[str, Any], max_rows: int) -> list[dict]:
        """Walk a query with ``startRow``, up to ``max_rows`` rows.

        Google returns the top rows by clicks, so a cap keeps the most relevant ones. A page
        shorter than asked for is the last one.
        """
        rows: list[dict] = []
        while len(rows) < max_rows:
            wanted = min(ROW_LIMIT, max_rows - len(rows))
            page = self.query(site_url, {**body, "rowLimit": wanted, "startRow": len(rows)})
            rows.extend(page)
            if len(page) < wanted:
                break
        return rows

    def _request(self, method: str, path: str, body: dict[str, Any] | None = None) -> dict:
        """Send one request, retrying rate limits, transient errors and an expired token."""
        renewed = False
        for attempt in range(_MAX_RETRIES):
            last = attempt == _MAX_RETRIES - 1
            headers = {"Authorization": f"Bearer {self._credentials.token()}"}
            try:
                response = self._http.request(method, path, json=body, headers=headers)
            except httpx.TransportError as exc:
                if last:
                    raise
                self._wait(type(exc).__name__, attempt, None, None)
                continue
            if response.status_code == 401 and not renewed and self._credentials.can_refresh:
                renewed = True
                self._credentials.refresh()
                continue
            # Only a refusal is parsed here; a successful body can hold 25,000 rows.
            if not last and (
                response.status_code in _RETRY_STATUS
                or (response.status_code == 403 and _reason(response) in _RATE_LIMIT_REASONS)
            ):
                self._wait(f"HTTP {response.status_code}", attempt, response.headers, response)
                continue
            if response.status_code >= 400:
                raise _api_error(response)
            return _json_object(response)
        raise AssertionError("unreachable")  # pragma: no cover

    def _wait(
        self,
        reason: str,
        attempt: int,
        headers: httpx.Headers | None,
        response: httpx.Response | None,
    ) -> None:
        delay = _retry_after(headers, attempt)
        if delay > _MAX_WAIT or self._slept + delay > _SLEEP_BUDGET:
            # Stop cleanly: what was read is kept and the next run resumes.
            status = response.status_code if response is not None else 429
            raise SearchConsoleAPIError(
                status if status in (429, 403) else 429,
                f"Google asks to wait {delay:.0f}s; stopping so the next run can resume.",
                "rateLimitExceeded",
            )
        self._slept += delay
        logger.warning(
            "Search Console: %s, retrying in %.1fs (%d/%d).",
            reason,
            delay,
            attempt + 1,
            _MAX_RETRIES,
        )
        self._sleep(delay)


def _encode(site_url: str) -> str:
    """Percent-encode a property for the URL path, slashes and colons included."""
    return quote(site_url, safe="")


def _retry_after(headers: httpx.Headers | None, attempt: int) -> float:
    """Seconds to wait: the Retry-After header, else exponential backoff."""
    try:
        return max(0.0, float((headers or {}).get("retry-after")))
    except (TypeError, ValueError):
        return float(2**attempt)


def _json_object(response: httpx.Response) -> dict:
    """The body of a successful call. A proxy's HTML page must not read as "no rows"."""
    try:
        body = response.json()
    except ValueError:
        body = None
    if not isinstance(body, dict):
        raise SearchConsoleAPIError(
            response.status_code, "Google answered with something other than a JSON object."
        )
    return body


def _error_body(response: httpx.Response) -> dict:
    try:
        body = response.json()
    except ValueError:
        return {}
    error = body.get("error") if isinstance(body, dict) else None
    return error if isinstance(error, dict) else {}


def _reason(response: httpx.Response) -> str | None:
    errors = _error_body(response).get("errors")
    first = errors[0] if isinstance(errors, list) and errors else {}
    return first.get("reason") if isinstance(first, dict) else None


def _api_error(response: httpx.Response) -> SearchConsoleAPIError:
    message = _error_body(response).get("message")
    return SearchConsoleAPIError(
        response.status_code, message or response.reason_phrase, _reason(response)
    )
