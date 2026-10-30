"""QuickBooks Online API client with OAuth 2.0 refresh, query pagination, and CDC support."""

from __future__ import annotations

import math
import os
import time
from collections.abc import Iterator
from typing import Any

import requests
from requests.auth import HTTPBasicAuth

SANDBOX_BASE_URL = "https://sandbox-quickbooks.api.intuit.com"
PRODUCTION_BASE_URL = "https://quickbooks.api.intuit.com"
TOKEN_ENDPOINT = "https://oauth.platform.intuit.com/oauth2/v1/tokens/bearer"

DEFAULT_MAX_RETRIES = 4
DEFAULT_BACKOFF_FACTOR = 1.5


def _retry_after(headers: Any, attempt: int) -> float:
    """Extract and validate Retry-After header delay in seconds.

    Falls back to exponential backoff if the header is missing, non-numeric,
    negative, NaN, or infinite.
    """
    default_delay = float(DEFAULT_BACKOFF_FACTOR**attempt)
    if not headers or not hasattr(headers, "get"):
        return default_delay

    header_val = headers.get("Retry-After") or headers.get("retry-after")
    if header_val is None:
        return default_delay

    try:
        val = float(header_val)
        if math.isnan(val) or math.isinf(val) or val <= 0:
            return default_delay
        return val
    except (TypeError, ValueError):
        return default_delay


class QuickBooksError(Exception):
    """Base exception for QuickBooks connector."""


class QuickBooksAuthError(QuickBooksError):
    """Raised when authentication or token refresh fails."""


class QuickBooksRateLimitError(QuickBooksError):
    """Raised when QuickBooks API rate limit (429) is exceeded after maximum retries."""


class QuickBooksAPIError(QuickBooksError):
    """Raised when QuickBooks API returns a server or request error."""

    def __init__(
        self,
        message: str,
        status_code: int | None = None,
        response_body: str | None = None,
    ):
        super().__init__(message)
        self.status_code = status_code
        self.response_body = response_body


class QuickBooksClient:
    """HTTP client for QuickBooks Online Accounting API v3 with Bearer OAuth 2.0."""

    def __init__(
        self,
        realm_id: str | None = None,
        client_id: str | None = None,
        client_secret: str | None = None,
        refresh_token: str | None = None,
        access_token: str | None = None,
        sandbox: bool = True,
        session: requests.Session | None = None,
        max_retries: int = DEFAULT_MAX_RETRIES,
    ):
        self.realm_id = realm_id or os.environ.get("QB_REALM_ID")
        self.client_id = client_id or os.environ.get("QB_CLIENT_ID")
        self.client_secret = client_secret or os.environ.get("QB_CLIENT_SECRET")
        self.refresh_token = refresh_token or os.environ.get("QB_REFRESH_TOKEN")
        self.access_token = access_token or os.environ.get("QB_ACCESS_TOKEN")
        self.sandbox = sandbox
        self.max_retries = max_retries

        if not self.realm_id:
            raise ValueError("QuickBooks realm_id (Company ID) is required.")

        base_host = SANDBOX_BASE_URL if self.sandbox else PRODUCTION_BASE_URL
        self.api_base = f"{base_host}/v3/company/{self.realm_id}"
        self.session = session or requests.Session()

    def refresh_access_token(self) -> str:
        """Exchange refresh_token for a new bearer access_token."""
        if not (self.client_id and self.client_secret and self.refresh_token):
            raise QuickBooksAuthError(
                "client_id, client_secret, and refresh_token are required to refresh access token."
            )

        payload = {
            "grant_type": "refresh_token",
            "refresh_token": self.refresh_token,
        }
        headers = {
            "Accept": "application/json",
            "Content-Type": "application/x-www-form-urlencoded",
        }

        try:
            resp = self.session.post(
                TOKEN_ENDPOINT,
                data=payload,
                headers=headers,
                auth=HTTPBasicAuth(self.client_id, self.client_secret),
                timeout=30,
            )
        except requests.RequestException as exc:
            raise QuickBooksAuthError(f"Token refresh request failed: {exc}") from exc

        if resp.status_code != 200:
            raise QuickBooksAuthError(f"OAuth refresh failed ({resp.status_code}): {resp.text}")

        data = resp.json()
        new_access = data.get("access_token")
        new_refresh = data.get("refresh_token")

        if not new_access:
            raise QuickBooksAuthError(
                "QuickBooks did not return access_token in token refresh response."
            )

        self.access_token = new_access
        if new_refresh:
            self.refresh_token = new_refresh

        return self.access_token

    def _get_auth_headers(self) -> dict[str, str]:
        if not self.access_token and self.refresh_token:
            self.refresh_access_token()

        if not self.access_token:
            raise QuickBooksAuthError(
                "No access token available. Provide access_token or refresh_token."
            )

        return {
            "Authorization": f"Bearer {self.access_token}",
            "Accept": "application/json",
            "Content-Type": "application/json",
        }

    def request(
        self,
        method: str,
        path_or_url: str,
        params: dict[str, Any] | None = None,
        data: Any = None,
        json: Any = None,
        retried_auth: bool = False,
    ) -> dict[str, Any]:
        """Send authenticated request to QuickBooks V3 API with retry logic."""
        url = (
            path_or_url
            if path_or_url.startswith("http")
            else f"{self.api_base}/{path_or_url.lstrip('/')}"
        )
        headers = self._get_auth_headers()

        for attempt in range(self.max_retries + 1):
            try:
                resp = self.session.request(
                    method=method,
                    url=url,
                    headers=headers,
                    params=params,
                    data=data,
                    json=json,
                    timeout=30,
                )
            except requests.RequestException as exc:
                if attempt == self.max_retries:
                    raise QuickBooksAPIError(
                        f"Network error communicating with QuickBooks: {exc}"
                    ) from exc
                time.sleep(DEFAULT_BACKOFF_FACTOR**attempt)
                continue

            # Automatic token re-authentication on 401
            if resp.status_code == 401 and not retried_auth and self.refresh_token:
                try:
                    self.refresh_access_token()
                    return self.request(
                        method=method,
                        path_or_url=path_or_url,
                        params=params,
                        data=data,
                        json=json,
                        retried_auth=True,
                    )
                except Exception as refresh_exc:
                    raise QuickBooksAuthError(
                        f"Authentication failed on 401 retry: {refresh_exc}"
                    ) from refresh_exc

            # Rate limit backoff
            if resp.status_code == 429:
                if attempt == self.max_retries:
                    raise QuickBooksRateLimitError("QuickBooks API rate limit exceeded.")
                delay = _retry_after(resp.headers, attempt)
                time.sleep(delay)
                continue

            # Server error retry
            if resp.status_code in (500, 502, 503, 504):
                if attempt == self.max_retries:
                    raise QuickBooksAPIError(
                        f"QuickBooks server error {resp.status_code}: {resp.text}",
                        status_code=resp.status_code,
                        response_body=resp.text,
                    )
                time.sleep(DEFAULT_BACKOFF_FACTOR**attempt)
                continue

            if not (200 <= resp.status_code < 300):
                raise QuickBooksAPIError(
                    f"QuickBooks API returned error {resp.status_code}: {resp.text}",
                    status_code=resp.status_code,
                    response_body=resp.text,
                )

            return resp.json()

        raise QuickBooksAPIError("Max retries exceeded.")

    def query(self, query_str: str) -> list[dict[str, Any]]:
        """Run a QuickBooks Online query (SQL-like syntax)."""
        data = self.request("GET", "query", params={"query": query_str})
        query_response = data.get("QueryResponse", {})
        for _key, val in query_response.items():
            if isinstance(val, list):
                return val
        return []

    def fetch_all(
        self,
        entity_name: str,
        last_updated_time: str | None = None,
        page_size: int = 100,
    ) -> Iterator[dict[str, Any]]:
        """Paginate through all entities, optionally filtering by LastUpdatedTime."""
        start_position = 1

        while True:
            if last_updated_time:
                # Query syntax: Metadata.LastUpdatedTime >= 'YYYY-MM-DDTHH:MM:SSZ'
                # Standard Intuit QBO syntax: ORDER BY with space
                q = (
                    f"SELECT * FROM {entity_name} "
                    f"WHERE Metadata.LastUpdatedTime >= '{last_updated_time}' "
                    f"ORDER BY Metadata.LastUpdatedTime "
                    f"STARTPOSITION {start_position} MAXRESULTS {page_size}"
                )
            else:
                q = (
                    f"SELECT * FROM {entity_name} "
                    f"STARTPOSITION {start_position} MAXRESULTS {page_size}"
                )

            items = self.query(q)
            if not items:
                break

            yield from items

            if len(items) < page_size:
                break

            start_position += len(items)

    def fetch_active_ids(self, entity_name: str, page_size: int = 500) -> set[str]:
        """Fetch all currently active entity IDs via lightweight ID projection queries."""
        active_ids: set[str] = set()
        start_position = 1

        while True:
            q = (
                f"SELECT Id FROM {entity_name} "
                f"STARTPOSITION {start_position} MAXRESULTS {page_size}"
            )
            items = self.query(q)
            if not items:
                break

            for item in items:
                if "Id" in item:
                    active_ids.add(str(item["Id"]))

            if len(items) < page_size:
                break

            start_position += len(items)

        return active_ids

    def fetch_cdc(self, entities: list[str], changed_since: str) -> list[dict[str, Any]]:
        """Query Change Data Capture (CDC) endpoint for modified and deleted items.

        Returns list of entity records, including deleted records with status="Deleted".
        """
        entities_param = ",".join(entities)
        data = self.request(
            "GET", "cdc", params={"entities": entities_param, "changedSince": changed_since}
        )
        cdc_response = data.get("CDCResponse", [])

        records: list[dict[str, Any]] = []
        for container in cdc_response:
            query_response = container.get("QueryResponse", [])
            for qr in query_response:
                for key, val in qr.items():
                    if isinstance(val, list):
                        for item in val:
                            item["_entity_type"] = key
                            records.append(item)

        return records
