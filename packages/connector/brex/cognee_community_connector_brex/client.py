"""Brex REST API client with Bearer authentication and pagination handling."""

from __future__ import annotations

import datetime
import time
from collections.abc import Iterator
from email.utils import parsedate_to_datetime
from typing import Any

import requests

DEFAULT_BASE_URL = "https://api.brex.com"
DEFAULT_TIMEOUT = 30
DEFAULT_MAX_RETRY_SLEEP = 60.0


class BrexAPIError(Exception):
    """Base exception for Brex API errors."""

    def __init__(
        self, message: str, status_code: int | None = None, response_body: str | None = None
    ):
        super().__init__(message)
        self.status_code = status_code
        self.response_body = response_body


class BrexAuthError(BrexAPIError):
    """Raised when Brex API token is invalid or unauthorized (HTTP 401)."""


class BrexPermissionError(BrexAPIError):
    """Raised when Brex token lacks required scope or permissions (HTTP 403)."""


class BrexRateLimitError(BrexAPIError):
    """Raised when Brex rate limits are exceeded (HTTP 429)."""


def _parse_retry_after(retry_header: str | None, default_backoff: float) -> float:
    """Parse Retry-After header safely handling numeric seconds and RFC 7231 HTTP-dates."""
    if not retry_header or not retry_header.strip():
        return default_backoff

    cleaned = retry_header.strip()

    # 1. Try integer or float seconds
    try:
        seconds = float(cleaned)
        return min(max(0.0, seconds), DEFAULT_MAX_RETRY_SLEEP)
    except ValueError:
        pass

    # 2. Try RFC 7231 / RFC 2822 HTTP-date format (e.g. "Wed, 21 Oct 2026 07:28:00 GMT")
    try:
        target_dt = parsedate_to_datetime(cleaned)
        now_dt = datetime.datetime.now(datetime.UTC)
        delta = (target_dt - now_dt).total_seconds()
        return min(max(0.0, delta), DEFAULT_MAX_RETRY_SLEEP)
    except Exception:
        return default_backoff


class BrexClient:
    """HTTP client for official Brex API (v1 expenses, v2 budgets)."""

    def __init__(
        self,
        token: str,
        base_url: str = DEFAULT_BASE_URL,
        session: requests.Session | None = None,
        timeout: int = DEFAULT_TIMEOUT,
        max_retries: int = 3,
    ) -> None:
        if not token:
            raise ValueError("Brex API token is required.")

        self.token = token
        self.base_url = base_url.rstrip("/")
        self.session = session or requests.Session()
        self.timeout = timeout
        self.max_retries = max_retries

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.token}",
            "Accept": "application/json",
            "Content-Type": "application/json",
            "User-Agent": "cognee-community-connector-brex/0.1.0",
        }

    def _request(
        self,
        method: str,
        endpoint: str,
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Execute HTTP request with retry logic for transient errors and rate limits."""
        url = f"{self.base_url}/{endpoint.lstrip('/')}"
        clean_params = {k: v for k, v in (params or {}).items() if v is not None}

        retries = 0
        backoff = 1.0

        while True:
            try:
                response = self.session.request(
                    method=method,
                    url=url,
                    headers=self._headers(),
                    params=clean_params,
                    timeout=self.timeout,
                )

                if response.status_code == 401:
                    raise BrexAuthError(
                        "Brex authentication failed (HTTP 401). "
                        "Verify your BREX_API_TOKEN is valid.",
                        status_code=401,
                        response_body=response.text,
                    )

                if response.status_code == 403:
                    raise BrexPermissionError(
                        "Brex access forbidden (HTTP 403). "
                        "Ensure token has expenses.read and budgets.read scopes.",
                        status_code=403,
                        response_body=response.text,
                    )

                if response.status_code == 429:
                    retry_after = response.headers.get("Retry-After")
                    sleep_time = _parse_retry_after(retry_after, backoff)
                    if retries < self.max_retries:
                        retries += 1
                        time.sleep(sleep_time)
                        backoff *= 2
                        continue
                    raise BrexRateLimitError(
                        f"Brex rate limit exceeded (HTTP 429) after {retries} retries.",
                        status_code=429,
                        response_body=response.text,
                    )

                if response.status_code >= 500:
                    if retries < self.max_retries:
                        retries += 1
                        time.sleep(backoff)
                        backoff *= 2
                        continue
                    raise BrexAPIError(
                        f"Brex server error (HTTP {response.status_code}).",
                        status_code=response.status_code,
                        response_body=response.text,
                    )

                response.raise_for_status()
                return response.json()

            except (requests.ConnectionError, requests.Timeout) as exc:
                if retries < self.max_retries:
                    retries += 1
                    time.sleep(backoff)
                    backoff *= 2
                    continue
                raise BrexAPIError(f"Network error communicating with Brex API: {exc}") from exc

    def fetch_expenses(
        self,
        updated_at_start: str | None = None,
        posted_at_start: str | None = None,
        cursor: str | None = None,
        limit: int = 50,
    ) -> dict[str, Any]:
        """Fetch a single page of expenses from Brex API (GET /v1/expenses).

        Args:
            updated_at_start: RFC 3339 timestamp filter for incremental sync (captures edits).
            posted_at_start: Backward-compatible alias for incremental sync cursor.
            cursor: Opaque pagination cursor.
            limit: Page size (1 to 100).
        """
        if not (1 <= limit <= 100):
            raise ValueError(f"limit must be between 1 and 100, got {limit}.")

        sync_cursor = updated_at_start or posted_at_start
        params: dict[str, Any] = {
            "limit": limit,
            "cursor": cursor,
            "updated_at_start": sync_cursor,
        }
        return self._request("GET", "v1/expenses", params=params)

    def fetch_all_expenses(
        self,
        updated_at_start: str | None = None,
        posted_at_start: str | None = None,
        limit: int = 50,
    ) -> Iterator[dict[str, Any]]:
        """Iterate through all expenses across pages using cursor pagination."""
        cursor: str | None = None
        while True:
            data = self.fetch_expenses(
                updated_at_start=updated_at_start,
                posted_at_start=posted_at_start,
                cursor=cursor,
                limit=limit,
            )
            items = data.get("items") or []
            yield from items

            cursor = data.get("next_cursor")
            if not cursor:
                break

    def fetch_budgets(
        self,
        cursor: str | None = None,
        limit: int = 50,
    ) -> dict[str, Any]:
        """Fetch a single page of budget hierarchies from Brex Budgets API (GET /v2/budgets).

        Args:
            cursor: Opaque pagination cursor.
            limit: Page size (1 to 100).
        """
        if not (1 <= limit <= 100):
            raise ValueError(f"limit must be between 1 and 100, got {limit}.")

        params: dict[str, Any] = {
            "limit": limit,
            "cursor": cursor,
        }
        return self._request("GET", "v2/budgets", params=params)

    def fetch_all_budgets(self, limit: int = 50) -> Iterator[dict[str, Any]]:
        """Iterate through all budgets across pages."""
        cursor: str | None = None
        while True:
            data = self.fetch_budgets(cursor=cursor, limit=limit)
            items = data.get("items") or []
            yield from items

            cursor = data.get("next_cursor")
            if not cursor:
                break
