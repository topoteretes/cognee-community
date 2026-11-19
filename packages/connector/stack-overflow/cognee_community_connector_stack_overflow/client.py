"""Stack Exchange API client with quota tracking and backoff.

Injectable via the ``client=`` parameter of ``stack_overflow_source`` so
CI tests never make live network calls.
"""

from __future__ import annotations

import os
import time
import logging
from typing import Any, Iterator

import requests

logger = logging.getLogger("stack_overflow_connector.client")

_BASE_URL = "https://api.stackexchange.com/2.3"
_SITE = "stackoverflow"
# One filter that returns body + answers in a single call.
# Create via https://api.stackexchange.com/docs/filters
# This is a reasonable default; generate a custom one for production.
_DEFAULT_FILTER = "!nNPvSNdWme"


class QuotaExhaustedError(Exception):
    """Raised when the Stack Exchange daily quota is fully consumed."""


class StackOverflowClient:
    """Thin wrapper around the Stack Exchange v2.3 REST API.

    Args:
        api_key: Stack Exchange API key (10 000 req/day). Falls back to
            ``STACKOVERFLOW_API_KEY`` env-var. Without a key the limit is
            300 req/day.
        max_requests_per_run: Hard cap on requests this run. ``None`` = unlimited.
        site: Stack Exchange site name (default ``stackoverflow``).
        filter_id: Stack Exchange filter ID that includes ``body`` and ``answers``.
    """

    def __init__(
        self,
        api_key: str | None = None,
        max_requests_per_run: int | None = None,
        site: str = _SITE,
        filter_id: str = _DEFAULT_FILTER,
    ) -> None:
        self._api_key = api_key or os.environ.get("STACKOVERFLOW_API_KEY")
        self._site = site
        self._filter_id = filter_id
        self._max_requests = max_requests_per_run
        self._requests_used: int = 0
        self._quota_remaining: int | None = None
        self._session = requests.Session()
        self._session.headers.update({"Accept-Encoding": "gzip"})

    # ------------------------------------------------------------------
    # Public helpers
    # ------------------------------------------------------------------

    @property
    def requests_used(self) -> int:
        return self._requests_used

    @property
    def quota_remaining(self) -> int | None:
        return self._quota_remaining

    def budget_ok(self) -> bool:
        """Return True if we have budget for at least one more request."""
        if self._max_requests is not None and self._requests_used >= self._max_requests:
            return False
        if self._quota_remaining is not None and self._quota_remaining <= 0:
            return False
        return True

    def get_questions_by_tag(
        self,
        tag: str,
        from_date: int | None = None,
        min_score: int = 0,
        page_size: int = 100,
    ) -> Iterator[dict[str, Any]]:
        """Yield question dicts (with body + answers) for *tag*, newest activity first."""
        page = 1
        while True:
            params: dict[str, Any] = {
                "tagged": tag,
                "sort": "activity",
                "order": "asc",
                "pagesize": page_size,
                "page": page,
                "min": min_score,
                "filter": self._filter_id,
            }
            if from_date is not None:
                params["fromdate"] = from_date

            data = self._get("/questions", params)
            for item in data.get("items", []):
                yield item

            if not data.get("has_more", False):
                break
            page += 1

    def get_questions_by_ids(
        self, ids: list[int], page_size: int = 100
    ) -> Iterator[dict[str, Any]]:
        """Yield question dicts for a list of IDs (used for deletion recheck)."""
        for chunk_start in range(0, len(ids), page_size):
            chunk = ids[chunk_start : chunk_start + page_size]
            id_str = ";".join(str(i) for i in chunk)
            params: dict[str, Any] = {
                "filter": self._filter_id,
            }
            data = self._get(f"/questions/{id_str}", params)
            for item in data.get("items", []):
                yield item

    def estimate_page_count(self, tag: str, page_size: int = 100) -> int:
        """Return the estimated total number of questions for *tag*."""
        params: dict[str, Any] = {
            "tagged": tag,
            "sort": "activity",
            "order": "desc",
            "pagesize": 1,
            "filter": "total",
        }
        try:
            data = self._get("/questions", params)
            return data.get("total", 0)
        except Exception:
            return 0

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _get(self, path: str, params: dict[str, Any]) -> dict[str, Any]:
        if not self.budget_ok():
            raise QuotaExhaustedError(
                f"Request budget exhausted: {self._requests_used} requests used, "
                f"{self._quota_remaining} quota remaining."
            )

        url = _BASE_URL + path
        if self._api_key:
            params = {**params, "key": self._api_key}
        params = {**params, "site": self._site}

        for attempt in range(5):
            resp = self._session.get(url, params=params, timeout=30)
            self._requests_used += 1

            if resp.status_code == 429 or resp.status_code == 503:
                backoff = int(resp.headers.get("backoff", 2 ** (attempt + 1)))
                logger.warning("Rate limited; sleeping %ds", backoff)
                time.sleep(backoff)
                continue

            resp.raise_for_status()
            data = resp.json()

            # Honor explicit backoff field from the response body.
            if "backoff" in data:
                logger.info("API backoff requested: %ds", data["backoff"])
                time.sleep(data["backoff"])

            self._quota_remaining = data.get("quota_remaining")
            if self._quota_remaining is not None and self._quota_remaining <= 0:
                raise QuotaExhaustedError("Stack Exchange daily quota exhausted.")

            return data

        raise RuntimeError(f"Stack Exchange API request failed after 5 retries: {path}")