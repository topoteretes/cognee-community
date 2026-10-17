"""A small client for the Webflow Data API v2: bearer auth, retries, paginated reads."""

import os
import time
from collections.abc import Callable, Iterator
from typing import Any

import httpx
from cognee.shared.logging_utils import get_logger

logger = get_logger("webflow_connector")

API_URL = "https://api.webflow.com/v2"
TOKEN_ENV_VAR = "WEBFLOW_API_TOKEN"

# Webflow caps every list endpoint at 100 per page.
PAGE_SIZE = 100
# Retry budget for rate limits (429), server errors and network failures.
_MAX_RETRIES = 5
_RETRY_STATUS = {429, 500, 502, 503, 504}
# A listing that changes while it is read can skip or repeat rows; read it
# again a few times before giving up.
_LISTING_ATTEMPTS = 3


class WebflowAPIError(Exception):
    """A Webflow API call failed for good (not a retried rate limit or blip)."""

    def __init__(self, status: int, message: str, code: str | None = None):
        super().__init__(f"Webflow API error {status}{f' ({code})' if code else ''}: {message}")
        self.status = status
        self.code = code
        self.message = message


class WebflowClient:
    """Calls the Webflow Data API with the given token.

    Args:
        token: A site token or an OAuth access token. Falls back to
            ``WEBFLOW_API_TOKEN``.
        base_url: API root; override for a proxy or the CDN host.
        transport: An ``httpx`` transport, mainly a test-injection point.
        sleep: Called to wait out rate limits and backoff.
    """

    def __init__(
        self,
        token: str | None = None,
        *,
        base_url: str = API_URL,
        transport: httpx.BaseTransport | None = None,
        sleep: Callable[[float], None] = time.sleep,
        timeout: float = 30.0,
    ):
        token = token or os.environ.get(TOKEN_ENV_VAR)
        if not token:
            raise ValueError(f"Webflow token required: pass token= or set {TOKEN_ENV_VAR}.")
        self._sleep = sleep
        self._http = httpx.Client(
            base_url=base_url.rstrip("/"),
            headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
            transport=transport,
            timeout=timeout,
        )

    def close(self) -> None:
        self._http.close()

    def get(self, path: str, params: dict[str, Any] | None = None) -> dict:
        """GET one resource, retrying rate limits and transient errors."""
        for attempt in range(_MAX_RETRIES):
            last = attempt == _MAX_RETRIES - 1
            try:
                response = self._http.get(path, params=params)
            except httpx.TransportError as exc:
                if last:
                    raise
                self._wait(f"{type(exc).__name__}", attempt, None)
                continue
            if response.status_code in _RETRY_STATUS and not last:
                self._wait(f"HTTP {response.status_code}", attempt, response.headers)
                continue
            if response.status_code >= 400:
                raise _api_error(response)
            return response.json()
        raise AssertionError("unreachable")  # pragma: no cover

    def paginate(self, path: str, key: str, params: dict[str, Any] | None = None) -> Iterator[dict]:
        """Yield ``response[key]`` across offset pagination.

        Raises when Webflow reports more rows than it returned, so the caller
        never mistakes a short read for a complete one.
        """
        for batch, _total in self._pages(path, key, params):
            yield from batch

    def list_all(self, path: str, key: str, params: dict[str, Any] | None = None) -> list[dict]:
        """Read a whole listing that must be complete and free of repeats.

        A row created or deleted while an offset-paginated listing is read
        shifts the pages, so a row may be skipped or seen twice. The listing is
        read again when the reported total changed during the read or the
        distinct row count does not match it.
        """
        for attempt in range(_LISTING_ATTEMPTS):
            rows: list[dict] = []
            totals: list[int | None] = []
            for batch, total in self._pages(path, key, params):
                rows.extend(batch)
                totals.append(total)
            # A localized item can appear once per locale under one id.
            distinct = len({(row.get("id"), row.get("cmsLocaleId")) for row in rows})
            total = totals[-1]
            if total is None or (set(totals) == {total} and distinct == total):
                return rows
            logger.warning(
                "Webflow: %s changed while it was read (%d distinct rows, totals %s); reading it "
                "again (%d/%d).",
                path,
                distinct,
                sorted({t for t in totals if t is not None}),
                attempt + 1,
                _LISTING_ATTEMPTS,
            )
        raise WebflowAPIError(
            200, f"{path} kept changing while it was read; nothing was synced. Try again."
        )

    def _pages(
        self, path: str, key: str, params: dict[str, Any] | None
    ) -> Iterator[tuple[list[dict], int | None]]:
        """Yield ``(rows, reported_total)`` for each page until the listing ends."""
        offset = 0
        while True:
            page = self.get(path, {**(params or {}), "limit": PAGE_SIZE, "offset": offset})
            rows = page.get(key) or []
            total = (page.get("pagination") or {}).get("total")
            yield rows, total
            offset += len(rows)
            if total is None:
                if len(rows) < PAGE_SIZE:
                    return
            elif offset >= total:
                return
            elif not rows:
                raise WebflowAPIError(
                    200, f"{path} returned {offset} of {total} rows; the listing is incomplete."
                )

    def _wait(self, reason: str, attempt: int, headers: httpx.Headers | None) -> None:
        delay = _retry_after(headers, attempt)
        logger.warning(
            "Webflow: %s, retrying in %.1fs (%d/%d).", reason, delay, attempt + 1, _MAX_RETRIES
        )
        self._sleep(delay)


def _retry_after(headers: httpx.Headers | None, attempt: int) -> float:
    """Seconds to wait: the Retry-After header, else exponential backoff."""
    try:
        return max(0.0, float((headers or {}).get("retry-after")))
    except (TypeError, ValueError):
        return float(2**attempt)


def _api_error(response: httpx.Response) -> WebflowAPIError:
    try:
        body = response.json()
    except ValueError:
        body = {}
    message = body.get("message") if isinstance(body, dict) else None
    code = body.get("code") if isinstance(body, dict) else None
    return WebflowAPIError(response.status_code, message or response.reason_phrase, code)
