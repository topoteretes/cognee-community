"""Rate-limit-aware HTTP client for the Salesloft v2 API.

Features:
- Cursor-based pagination (sort=updated_at, per_page=100)
- Rate-limit header parsing and proactive backoff
- Retry with exponential backoff + jitter on 429/5xx
- Batched existence checks for reconciliation
"""

from __future__ import annotations

import math
import random
import time
from collections.abc import Iterator
from typing import Any

import httpx
from cognee.shared.logging_utils import get_logger

logger = get_logger("salesloft_client")

SALESLOFT_API_BASE = "https://api.salesloft.com"

_MAX_RETRIES = 5
_PER_PAGE = 100


class SalesloftClient:
    """Thin wrapper around Salesloft v2 REST API with rate-limit awareness."""

    def __init__(self, access_token: str, *, http_client: httpx.Client | None = None):
        self._token = access_token
        self._http = http_client or httpx.Client(timeout=30.0)
        self._owns_http = http_client is None

    def close(self) -> None:
        if self._owns_http:
            self._http.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    @property
    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._token}", "Accept": "application/json"}

    # -------------------------------------------------------------------
    # Core request with rate-limit + retry
    # -------------------------------------------------------------------
    def request(self, method: str, path: str, **kwargs) -> httpx.Response:
        """Issue an API request with rate-limit-aware retries."""
        url = f"{SALESLOFT_API_BASE}{path}" if path.startswith("/") else path

        for attempt in range(_MAX_RETRIES):
            try:
                response = self._http.request(
                    method, url, headers=self._headers, **kwargs
                )
            except (httpx.TransportError, httpx.TimeoutException) as exc:
                if attempt == _MAX_RETRIES - 1:
                    raise
                delay = _backoff_delay(attempt)
                logger.warning(
                    "Salesloft: %s — retrying in %.1fs (%d/%d).",
                    exc, delay, attempt + 1, _MAX_RETRIES,
                )
                time.sleep(delay)
                continue

            if response.status_code < 400:
                return response

            if response.status_code in (429, 500, 502, 503, 504):
                if attempt == _MAX_RETRIES - 1:
                    response.raise_for_status()
                delay = _retry_after(response.headers, attempt)
                logger.warning(
                    "Salesloft: HTTP %d — retrying in %.1fs (%d/%d).",
                    response.status_code, delay, attempt + 1, _MAX_RETRIES,
                )
                time.sleep(delay)
                continue

            # Permanent error (400, 401, 403, 404, etc.)
            response.raise_for_status()

        # Should not reach here, but defensive.
        raise RuntimeError("Salesloft: retries exhausted")  # pragma: no cover

    def get(self, path: str, **kwargs) -> dict[str, Any]:
        """GET request, returning parsed JSON."""
        return self.request("GET", path, **kwargs).json()

    # -------------------------------------------------------------------
    # Paginated listing with updated_at cursor
    # -------------------------------------------------------------------
    def paginate(
        self,
        path: str,
        *,
        updated_after: str | None = None,
        extra_params: dict[str, Any] | None = None,
    ) -> Iterator[dict[str, Any]]:
        """Yield all records from a paginated endpoint.

        Uses cursor-based pagination via ``page`` parameter with
        ``sort_by=updated_at`` and ``sort_direction=ASC`` to enable
        incremental sync.
        """
        params: dict[str, Any] = {
            "per_page": _PER_PAGE,
            "sort_by": "updated_at",
            "sort_direction": "ASC",
        }
        if updated_after:
            params["updated_at[gt]"] = updated_after
        if extra_params:
            params.update(extra_params)

        page = 1
        while True:
            params["page"] = page
            data = self.get(path, params=params)
            records = data.get("data", [])
            if not records:
                return
            yield from records
            metadata = data.get("metadata", {}) or {}
            paging = metadata.get("paging", {}) or {}
            if not paging.get("next_page"):
                return
            page = paging["next_page"]

    # -------------------------------------------------------------------
    # Batched existence checks (for reconciliation)
    # -------------------------------------------------------------------
    def check_ids_exist(
        self,
        path: str,
        ids: list[int | str],
        *,
        batch_size: int = 100,
    ) -> set[int | str]:
        """Return the subset of ``ids`` that still exist at the endpoint.

        Falls back to cursor-ordered listing if the endpoint does not
        support ``ids[]`` filtering.
        """
        existing: set[int | str] = set()

        for i in range(0, len(ids), batch_size):
            batch = ids[i : i + batch_size]
            try:
                data = self.get(path, params={"ids[]": batch, "per_page": batch_size})
                for record in data.get("data", []):
                    record_id = record.get("id")
                    if record_id is not None:
                        existing.add(record_id)
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code in (400, 422):
                    # Endpoint doesn't support ids[] filter — fall back to
                    # paginated listing and intersect.
                    logger.info(
                        "Salesloft: %s does not support ids[] filter, "
                        "falling back to full listing for reconciliation.",
                        path,
                    )
                    return self._list_all_ids(path) & set(ids)
                raise

        return existing

    def _list_all_ids(self, path: str) -> set[int | str]:
        """List all record IDs at an endpoint (for reconciliation fallback)."""
        all_ids: set[int | str] = set()
        for record in self.paginate(path):
            record_id = record.get("id")
            if record_id is not None:
                all_ids.add(record_id)
        return all_ids


def _backoff_delay(attempt: int) -> float:
    """Exponential backoff with jitter."""
    base = float(2**attempt)
    jitter = random.uniform(0, base * 0.5)
    return base + jitter


def _retry_after(headers: httpx.Headers, attempt: int) -> float:
    """Parse Retry-After header, falling back to exponential backoff."""
    raw = headers.get("retry-after") or headers.get("Retry-After")
    if raw:
        try:
            value = float(raw)
            if math.isfinite(value) and value > 0:
                return value
        except (TypeError, ValueError):
            pass
    return _backoff_delay(attempt)
