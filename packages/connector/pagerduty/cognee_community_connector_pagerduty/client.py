"""PagerDuty REST API client with retry and rate-limit backoff."""

import time
from typing import Any

import requests


class PagerDutyClient:
    """Client for PagerDuty REST API v2."""

    def __init__(
        self,
        token: str,
        base_url: str = "https://api.pagerduty.com",
        max_retries: int = 4,
        backoff_factor: float = 1.0,
        session: requests.Session | None = None,
    ):
        if not token:
            raise ValueError("PagerDuty API token must be provided.")

        self.token = token
        self.base_url = base_url.rstrip("/")
        self.max_retries = max_retries
        self.backoff_factor = backoff_factor
        self.session = session or requests.Session()

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Token token={self.token}",
            "Accept": "application/vnd.pagerduty+json;version=2",
            "Content-Type": "application/json",
            "User-Agent": "cognee-community-connector-pagerduty/0.1.0",
        }

    def request(
        self,
        method: str,
        endpoint: str,
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Make an HTTP request with retry on rate limits (429) and transient errors."""
        url = f"{self.base_url}/{endpoint.lstrip('/')}"
        headers = self._headers()

        for attempt in range(self.max_retries):
            try:
                response = self.session.request(
                    method=method,
                    url=url,
                    headers=headers,
                    params=params,
                    timeout=30,
                )

                if response.status_code == 429:
                    retry_after = response.headers.get("Retry-After")
                    sleep_time = (
                        float(retry_after) if retry_after else self.backoff_factor * (2**attempt)
                    )
                    if attempt < self.max_retries - 1:
                        time.sleep(sleep_time)
                        continue

                if response.status_code >= 500 and attempt < self.max_retries - 1:
                    time.sleep(self.backoff_factor * (2**attempt))
                    continue

                response.raise_for_status()
                return response.json()

            except (requests.RequestException, requests.Timeout):
                if attempt >= self.max_retries - 1:
                    raise
                time.sleep(self.backoff_factor * (2**attempt))

        raise RuntimeError(f"Failed to fetch {url} after {self.max_retries} retries.")

    def get_incidents(
        self,
        since: str | None = None,
        until: str | None = None,
        statuses: list[str] | None = None,
        urgencies: list[str] | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> dict[str, Any]:
        """Fetch a page of incidents."""
        params: dict[str, Any] = {
            "limit": min(limit, 100),
            "offset": offset,
            "total": True,
            "time_zone": "UTC",
        }
        if since:
            params["since"] = since
        if until:
            params["until"] = until
        if statuses:
            params["statuses[]"] = statuses
        if urgencies:
            params["urgencies[]"] = urgencies

        return self.request("GET", "/incidents", params=params)

    def get_services(self, limit: int = 100, offset: int = 0) -> dict[str, Any]:
        """Fetch a page of services."""
        params = {
            "limit": min(limit, 100),
            "offset": offset,
            "total": True,
        }
        return self.request("GET", "/services", params=params)

    def get_incident_notes(self, incident_id: str) -> list[dict[str, Any]]:
        """Fetch notes attached to an incident."""
        data = self.request("GET", f"/incidents/{incident_id}/notes")
        return data.get("notes", [])
