"""Atlassian Opsgenie API v2 client for querying alerts, schedules, and escalation policies."""

import os
import time
from typing import Any
from urllib.parse import urlparse

import httpx

try:
    from cognee.shared.logging_utils import get_logger

    logger = get_logger("opsgenie_client")
except ImportError:
    import logging

    logger = logging.getLogger("opsgenie_client")

_MAX_RETRIES = 3
_DEFAULT_TIMEOUT = 30.0
_DEFAULT_BASE_URL = "https://api.opsgenie.com"


class OpsgenieError(Exception):
    """Base exception for Opsgenie connector errors."""


class OpsgenieAuthError(OpsgenieError):
    """Authentication or authorization failure against Opsgenie API."""


class OpsgenieNotFoundError(OpsgenieError):
    """Requested resource not found in Opsgenie."""


class OpsgenieClient:
    """Client for interacting with Atlassian Opsgenie REST API v2."""

    def __init__(
        self,
        api_key: str | None = None,
        base_url: str | None = None,
        verify_ssl: bool = True,
        timeout: float = _DEFAULT_TIMEOUT,
    ):
        self.api_key = api_key or os.environ.get("OPSGENIE_API_KEY")
        if not self.api_key:
            raise ValueError("Opsgenie API key required: pass api_key= or set OPSGENIE_API_KEY.")

        raw_url = base_url or os.environ.get("OPSGENIE_BASE_URL") or _DEFAULT_BASE_URL
        parsed = urlparse(raw_url)
        if parsed.scheme not in ("http", "https") or not parsed.netloc:
            raise ValueError(
                f"Invalid Opsgenie base URL '{raw_url}'. Must include http/https scheme and host."
            )

        self.base_url = raw_url.rstrip("/")
        self.verify_ssl = verify_ssl
        self.timeout = timeout

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"GenieKey {self.api_key}",
            "Accept": "application/json",
            "Content-Type": "application/json",
        }

    def _redact(self, text: str) -> str:
        if not text or not self.api_key:
            return text
        return text.replace(self.api_key, "[REDACTED]")

    def request(self, method: str, path: str, params: dict[str, Any] | None = None) -> Any:
        """Execute HTTP request with exponential backoff on transient errors."""
        url = f"{self.base_url}/{path.lstrip('/')}"

        for attempt in range(_MAX_RETRIES):
            try:
                with httpx.Client(verify=True, timeout=self.timeout) as client:
                    resp = client.request(
                        method=method,
                        url=url,
                        headers=self._headers(),
                        params=params,
                    )

                    if resp.status_code in (401, 403):
                        raise OpsgenieAuthError(
                            f"Opsgenie authentication failed ({resp.status_code}) on {path}"
                        )
                    if resp.status_code == 404:
                        raise OpsgenieNotFoundError(f"Opsgenie resource not found: {path}")
                    if resp.status_code in (429, 500, 502, 503, 504):
                        if attempt == _MAX_RETRIES - 1:
                            resp.raise_for_status()
                        delay = min(1.5 * (2**attempt), 60.0)
                        logger.warning(
                            "Opsgenie: received status %d on %s — retrying in %.1fs (%d/%d)",
                            resp.status_code,
                            path,
                            delay,
                            attempt + 1,
                            _MAX_RETRIES,
                        )
                        time.sleep(delay)
                        continue

                    resp.raise_for_status()
                    return resp.json()

            except (httpx.TimeoutException, httpx.NetworkError) as exc:
                if attempt == _MAX_RETRIES - 1:
                    raise OpsgenieError(
                        f"Network error communicating with Opsgenie: {exc}"
                    ) from exc
                delay = min(1.5 * (2**attempt), 60.0)
                logger.warning(
                    "Opsgenie: connection error on %s — retrying in %.1fs (%d/%d)",
                    path,
                    delay,
                    attempt + 1,
                    _MAX_RETRIES,
                )
                time.sleep(delay)

        raise OpsgenieError(f"Request to {path} failed after {_MAX_RETRIES} attempts")

    def get_alerts(
        self,
        query: str | None = None,
        limit: int = 100,
        sort: str = "createdAt",
        order: str = "desc",
    ) -> list[dict[str, Any]]:
        """List Opsgenie alerts matching optional query."""
        params: dict[str, Any] = {
            "limit": limit,
            "sort": sort,
            "order": order,
        }
        if query:
            params["query"] = query

        data = self.request("GET", "/v2/alerts", params=params)
        return data.get("data") or []

    def get_alert(self, alert_id: str) -> dict[str, Any]:
        """Fetch full details for an individual alert."""
        data = self.request("GET", f"/v2/alerts/{alert_id}")
        return data.get("data") or {}

    def get_schedules(self) -> list[dict[str, Any]]:
        """List on-call schedules."""
        data = self.request("GET", "/v2/schedules")
        return data.get("data") or []

    def get_schedule_on_calls(self, schedule_id: str) -> dict[str, Any]:
        """Get who is currently on-call for a specific schedule."""
        data = self.request("GET", f"/v2/schedules/{schedule_id}/on-calls")
        return data.get("data") or {}

    def get_escalations(self) -> list[dict[str, Any]]:
        """List escalation policies."""
        data = self.request("GET", "/v2/escalations")
        return data.get("data") or []
