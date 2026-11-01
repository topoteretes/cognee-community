"""Salesforce REST API and OAuth 2.0 client for cognee community connector.

Provides authenticated requests to Salesforce REST API endpoints, SOQL query
execution with automatic pagination, replication-based deleted-record lookups,
and automatic OAuth token refresh on session expiration (HTTP 401).
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any
from urllib.parse import quote

try:
    from cognee.shared.logging_utils import get_logger

    logger = get_logger("salesforce_client")
except ImportError:
    import logging

    logger = logging.getLogger("salesforce_client")

DEFAULT_API_VERSION = "v60.0"
DEFAULT_AUTH_URL = "https://login.salesforce.com"


class SalesforceAuthError(Exception):
    """Raised when authentication or token refresh fails."""


class SalesforceAPIError(Exception):
    """Raised when a Salesforce REST API request fails."""


class SalesforceClient:
    """Client for Salesforce REST API operations with automatic token refresh."""

    def __init__(
        self,
        *,
        instance_url: str | None = None,
        client_id: str | None = None,
        client_secret: str | None = None,
        refresh_token: str | None = None,
        access_token: str | None = None,
        username: str | None = None,
        password: str | None = None,
        security_token: str | None = None,
        auth_url: str = DEFAULT_AUTH_URL,
        api_version: str = DEFAULT_API_VERSION,
        session: Any = None,
    ):
        self.instance_url = (instance_url or "").rstrip("/")
        self.client_id = client_id
        self.client_secret = client_secret
        self.refresh_token = refresh_token
        self.access_token = access_token
        self.username = username
        self.password = password
        self.security_token = security_token
        self.auth_url = auth_url.rstrip("/")
        self.api_version = api_version
        self._session = session

        self._ensure_session()

    def _ensure_session(self) -> Any:
        if self._session is None:
            try:
                import requests
            except ImportError as exc:
                raise ImportError(
                    'The Salesforce connector requires "requests". Install with:\n'
                    '    pip install "cognee-community-connector-salesforce"'
                ) from exc
            self._session = requests.Session()
            self._session.headers.update({"Accept": "application/json"})
        return self._session

    def authenticate(self) -> None:
        """Authenticate with Salesforce using provided credentials.

        Supports direct access token, OAuth refresh token flow, and username-password flow.
        """
        # 1. Direct access token provided
        if self.access_token and self.instance_url:
            return

        # 2. OAuth refresh token flow
        if self.refresh_token and self.client_id and self.client_secret:
            self.refresh_access_token()
            return

        # 3. Username-password flow (for Developer/Sandbox orgs)
        if self.username and self.password and self.client_id and self.client_secret:
            self._login_with_password()
            return

        raise SalesforceAuthError(
            "Insufficient credentials provided for Salesforce authentication. "
            "Provide (instance_url + access_token), (client_id + client_secret + refresh_token), "
            "or (client_id + client_secret + username + password)."
        )

    def refresh_access_token(self) -> None:
        """Exchange the refresh token for a fresh access token."""
        if not (self.client_id and self.client_secret and self.refresh_token):
            raise SalesforceAuthError(
                "Cannot refresh token without client_id, client_secret, and refresh_token."
            )

        token_url = f"{self.auth_url}/services/oauth2/token"
        payload = {
            "grant_type": "refresh_token",
            "client_id": self.client_id,
            "client_secret": self.client_secret,
            "refresh_token": self.refresh_token,
        }

        try:
            response = self._session.post(token_url, data=payload)
            response.raise_for_status()
            data = response.json()
            self.access_token = data.get("access_token")
            if data.get("instance_url"):
                self.instance_url = data["instance_url"].rstrip("/")
            logger.info("Salesforce OAuth access token successfully refreshed.")
        except Exception as exc:
            # Never leak client_secret or tokens in exception details
            raise SalesforceAuthError("Failed to refresh Salesforce OAuth access token.") from exc

    def _login_with_password(self) -> None:
        """Authenticate using username, password, and security token."""
        token_url = f"{self.auth_url}/services/oauth2/token"
        full_password = f"{self.password}{self.security_token or ''}"
        payload = {
            "grant_type": "password",
            "client_id": self.client_id,
            "client_secret": self.client_secret,
            "username": self.username,
            "password": full_password,
        }

        try:
            response = self._session.post(token_url, data=payload)
            response.raise_for_status()
            data = response.json()
            self.access_token = data.get("access_token")
            if data.get("instance_url"):
                self.instance_url = data["instance_url"].rstrip("/")
            logger.info("Salesforce password authentication successful.")
        except Exception as exc:
            raise SalesforceAuthError(
                "Salesforce username/password authentication failed."
            ) from exc

    def _request(
        self,
        method: str,
        path_or_url: str,
        *,
        params: dict[str, Any] | None = None,
        json_data: dict[str, Any] | None = None,
        retry_on_401: bool = True,
    ) -> dict[str, Any]:
        """Make an authenticated HTTP request, retrying once on 401 if refresh is available."""
        if not self.access_token:
            self.authenticate()

        url = path_or_url if path_or_url.startswith("http") else f"{self.instance_url}{path_or_url}"

        headers = {
            "Authorization": f"Bearer {self.access_token}",
            "Accept": "application/json",
        }

        response = self._session.request(
            method=method,
            url=url,
            headers=headers,
            params=params,
            json=json_data,
        )

        if response.status_code == 401 and retry_on_401 and self.refresh_token:
            logger.warning("Salesforce access token expired (401). Attempting refresh...")
            self.refresh_access_token()
            return self._request(
                method,
                path_or_url,
                params=params,
                json_data=json_data,
                retry_on_401=False,
            )

        if not response.ok:
            error_msg = f"Salesforce API request failed ({response.status_code}): {response.text}"
            logger.error(
                "Salesforce API error on %s %s: HTTP %d",
                method,
                url,
                response.status_code,
            )
            raise SalesforceAPIError(error_msg)

        if response.status_code == 204 or not response.content:
            return {}

        return response.json()

    def query(self, soql: str) -> Iterator[dict[str, Any]]:
        """Execute a SOQL query and yield all records across pagination pages."""
        endpoint = f"/services/data/{self.api_version}/query/?q={quote(soql)}"
        data = self._request("GET", endpoint)

        while True:
            records = data.get("records") or []
            yield from records

            next_url = data.get("nextRecordsUrl")
            if not next_url or data.get("done") is True:
                break
            data = self._request("GET", next_url)

    def get_deleted(
        self,
        sobject: str,
        start_time: str,
        end_time: str,
    ) -> list[dict[str, Any]]:
        """Fetch records deleted in the given time window via replication endpoint.

        Args:
            sobject: Name of the Salesforce object (e.g., 'Account', 'Opportunity').
            start_time: ISO 8601 start timestamp in UTC (e.g. '2026-10-08T00:00:00Z').
            end_time: ISO 8601 end timestamp in UTC.

        Returns:
            List of deleted record entries: [{'id': '001...', 'deletedDate': '...'}]
        """
        endpoint = f"/services/data/{self.api_version}/sobjects/{sobject}/deleted/"
        params = {
            "start": start_time,
            "end": end_time,
        }

        try:
            data = self._request("GET", endpoint, params=params)
            return data.get("deletedRecords") or []
        except SalesforceAPIError as exc:
            # Some objects (e.g., Chatter FeedItem) may not support the replication API
            logger.warning(
                "Salesforce replication getDeleted not supported or failed for %s: %s",
                sobject,
                exc,
            )
            return []
