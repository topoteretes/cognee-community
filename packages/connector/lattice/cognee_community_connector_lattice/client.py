"""A small client for the Lattice Talent API: bearer auth, retries, cursor-paginated reads."""

import os
import time
from collections.abc import Callable
from typing import Any

import httpx
from cognee.shared.logging_utils import get_logger

logger = get_logger("lattice_connector")

API_KEY_ENV_VAR = "LATTICE_API_KEY"
REGION_ENV_VAR = "LATTICE_REGION"

# Lattice serves each workspace from the base URL of its data residency.
REGION_URLS = {
    "us": "https://api.latticehq.com",
    "emea": "https://api.emea.latticehq.com",
}
DEFAULT_REGION = "us"

# Lattice caps every list endpoint at 100 per page (the default is 10).
PAGE_SIZE = 100
# Retry budget for rate limits (429, 240 requests a minute), server errors and
# network failures.
_MAX_RETRIES = 5
_RETRY_STATUS = {429, 500, 502, 503, 504}
# Longest single wait for a Retry-After; a server asking for more fails the run
# cleanly instead of holding cognee's process-wide staging lock for minutes.
# Lattice's own example is 39 seconds.
_MAX_WAIT = 120.0
# Upper bound on pages in one listing (1,000,000 rows), so a cursor that keeps
# changing can never loop forever.
_MAX_PAGES = 10_000


class LatticeAPIError(Exception):
    """A Lattice API call failed for good (not a retried rate limit or blip)."""

    def __init__(self, status: int, message: str):
        super().__init__(f"Lattice API error {status}: {message}")
        self.status = status
        self.message = message


class LatticeClient:
    """Calls the Lattice Talent API with the given API key. Only ``GET`` is ever sent.

    Args:
        api_key: A Lattice API key. Falls back to ``LATTICE_API_KEY``. The key
            carries the privileges of the user who created it.
        region: Data residency of the workspace, ``"us"`` or ``"emea"``. Falls
            back to ``LATTICE_REGION``, then ``"us"``.
        base_url: API root; overrides ``region`` (for a proxy or a test server).
        transport: An ``httpx`` transport, mainly a test-injection point.
        sleep: Called to wait out rate limits and backoff.
        check_active: Called before every request; raises to stop a sync whose access
            was revoked mid-run (for hosts).
    """

    def __init__(
        self,
        api_key: str | None = None,
        *,
        region: str | None = None,
        base_url: str | None = None,
        transport: httpx.BaseTransport | None = None,
        sleep: Callable[[float], None] = time.sleep,
        timeout: float = 30.0,
        check_active: Callable[[], None] | None = None,
    ):
        # A pasted key often carries a trailing newline, which would make the HTTP
        # layer reject the header with a message that quotes it.
        api_key = (api_key or os.environ.get(API_KEY_ENV_VAR) or "").strip()
        if not api_key:
            raise ValueError(f"Lattice API key required: pass api_key= or set {API_KEY_ENV_VAR}.")
        if not api_key.isprintable() or any(ch.isspace() for ch in api_key):
            raise ValueError("The Lattice API key contains spaces or control characters.")
        self._sleep = sleep
        self.check_active = check_active
        self._open = lambda: httpx.Client(
            base_url=(base_url or region_url(region)).rstrip("/"),
            headers={"Authorization": f"Bearer {api_key}", "Accept": "application/json"},
            transport=transport,
            timeout=timeout,
        )
        self._http: httpx.Client | None = None

    def close(self) -> None:
        """Close the connection. The next request opens a new one."""
        if self._http is not None:
            self._http.close()
            self._http = None

    def get(self, path: str, params: dict[str, Any] | None = None) -> dict:
        """GET one resource, retrying rate limits and transient errors."""
        if self._http is None:
            self._http = self._open()
        for attempt in range(_MAX_RETRIES):
            last = attempt == _MAX_RETRIES - 1
            if self.check_active is not None:
                self.check_active()
            try:
                response = self._http.get(path, params=params)
            except httpx.TransportError as exc:
                if last:
                    raise
                self._wait(type(exc).__name__, attempt, None)
                continue
            if response.status_code in _RETRY_STATUS and not last:
                self._wait(
                    f"HTTP {response.status_code}", attempt, response.headers, response.status_code
                )
                continue
            if response.status_code >= 400:
                raise _api_error(response)
            return _json_object(response, path)
        raise AssertionError("unreachable")  # pragma: no cover

    def list_all(self, path: str, params: dict[str, Any] | None = None) -> list[dict]:
        """Read a whole listing across cursor pagination.

        Raises when a page has no ``data`` list, or when Lattice says there is more
        but gives no usable cursor, so the caller never mistakes a short read for a
        complete one (a partial listing would otherwise look like deletions).
        """
        rows: list[dict] = []
        cursor: str | None = None
        seen_cursors: set[str] = set()
        while True:
            query = {**(params or {}), "limit": PAGE_SIZE}
            if cursor:
                query["startingAfter"] = cursor
            page = self.get(path, query)
            data = page.get("data")
            if not isinstance(data, list):
                raise LatticeAPIError(200, f"{path} answered without a `data` list; not synced.")
            rows.extend(row for row in data if isinstance(row, dict))
            more, cursor = _next_cursor(page)
            if not more:
                return rows
            if not cursor or cursor in seen_cursors or len(seen_cursors) >= _MAX_PAGES:
                raise LatticeAPIError(
                    200,
                    f"{path} reports more rows but no new cursor after {len(rows)} rows; "
                    "the listing is incomplete.",
                )
            seen_cursors.add(cursor)

    def _wait(
        self, reason: str, attempt: int, headers: httpx.Headers | None, status: int = 0
    ) -> None:
        delay = _retry_after(headers, attempt)
        if delay > _MAX_WAIT:
            raise LatticeAPIError(
                status,
                f"{reason}: Lattice asked to wait {delay:.0f}s, longer than {_MAX_WAIT:.0f}s.",
            )
        logger.warning(
            "Lattice: %s, retrying in %.1fs (%d/%d).", reason, delay, attempt + 1, _MAX_RETRIES
        )
        self._sleep(delay)


def region_url(region: str | None = None) -> str:
    """The API root for a data-residency region."""
    name = (region or os.environ.get(REGION_ENV_VAR) or DEFAULT_REGION).strip().lower()
    if name not in REGION_URLS:
        raise ValueError(f"Unknown Lattice region {name!r}; use one of {sorted(REGION_URLS)}.")
    return REGION_URLS[name]


def _next_cursor(page: dict) -> tuple[bool, str | None]:
    """``(has_more, cursor)`` for either list shape Lattice uses.

    Most endpoints answer ``{hasMore, endingCursor}``; the goal-updates
    endpoints answer ``{pageInfo: {hasNextPage, endCursor}}``.
    """
    info = page.get("pageInfo")
    if isinstance(info, dict):
        return bool(info.get("hasNextPage")), info.get("endCursor")
    return bool(page.get("hasMore")), page.get("endingCursor")


def _retry_after(headers: httpx.Headers | None, attempt: int) -> float:
    """Seconds to wait: the Retry-After header, else exponential backoff."""
    try:
        return max(0.0, float((headers or {}).get("retry-after")))
    except (TypeError, ValueError):
        return float(2**attempt)


def _json_object(response: httpx.Response, path: str) -> dict:
    """The JSON object a 2xx answered with; anything else is a failed read, not an empty one."""
    try:
        body = response.json()
    except ValueError:
        body = None
    if not isinstance(body, dict):
        raise LatticeAPIError(response.status_code, f"{path} did not answer with a JSON object.")
    return body


def _api_error(response: httpx.Response) -> LatticeAPIError:
    try:
        body = response.json()
    except ValueError:
        body = {}
    message = body.get("error") if isinstance(body, dict) else None
    if response.status_code == 401:
        message = f"{message or 'unauthorized'} (check the API key and the region)"
    return LatticeAPIError(response.status_code, message or response.reason_phrase)
