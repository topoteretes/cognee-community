"""Databricks Unity Catalog REST API client for governance, tables, and AI models."""

import os
import time
from typing import Any
from urllib.parse import urlparse

import httpx

try:
    from cognee.shared.logging_utils import get_logger

    logger = get_logger("unitycatalog_client")
except ImportError:
    import logging

    logger = logging.getLogger("unitycatalog_client")

_MAX_RETRIES = 3
_DEFAULT_TIMEOUT = 30.0


class UnityCatalogError(Exception):
    """Base exception for Unity Catalog connector errors."""


class UnityCatalogAuthError(UnityCatalogError):
    """Authentication or authorization failure against Unity Catalog API."""


class UnityCatalogNotFoundError(UnityCatalogError):
    """Requested catalog, schema, table, or model not found in Unity Catalog."""


class UnityCatalogClient:
    """Client for interacting with the Databricks or Open-Source Unity Catalog REST API."""

    def __init__(
        self,
        base_url: str | None = None,
        token: str | None = None,
        verify_ssl: bool = True,
        timeout: float = _DEFAULT_TIMEOUT,
    ):
        raw_url = (
            base_url
            or os.environ.get("DATABRICKS_HOST")
            or os.environ.get("UNITY_CATALOG_URL")
            or os.environ.get("DATABRICKS_SERVER_URL")
        )
        if not raw_url:
            raise ValueError("Unity Catalog URL required: pass base_url= or set DATABRICKS_HOST.")

        parsed = urlparse(raw_url)
        if parsed.scheme not in ("http", "https") or not parsed.netloc:
            raise ValueError(f"Invalid Unity Catalog URL '{raw_url}'. Scheme and host required.")

        cleaned = raw_url.rstrip("/")
        # If user supplied full endpoint path by accident, trim it back to host root
        if "/api/2.1/unity-catalog" in cleaned:
            cleaned = cleaned.split("/api/2.1/unity-catalog")[0]
        self.base_url = cleaned

        self.token = (
            token or os.environ.get("DATABRICKS_TOKEN") or os.environ.get("UNITY_CATALOG_TOKEN")
        )
        if not self.token:
            raise ValueError(
                "Unity Catalog authentication token required: pass token= or set DATABRICKS_TOKEN."
            )

        self.verify_ssl = verify_ssl
        self.timeout = timeout

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.token}",
            "Accept": "application/json",
            "Content-Type": "application/json",
        }

    def redact(self, text: str) -> str:
        """Scrub secret credentials from logs and exception messages."""
        if not text or not self.token:
            return text
        return text.replace(self.token, "[REDACTED]")

    def request(
        self,
        method: str,
        path: str,
        params: dict[str, Any] | None = None,
    ) -> Any:
        """Execute HTTP request with exponential backoff on transient errors."""
        endpoint = path.lstrip("/")
        url = f"{self.base_url}/{endpoint}"

        for attempt in range(_MAX_RETRIES):
            try:
                with httpx.Client(verify=self.verify_ssl, timeout=self.timeout) as client:
                    resp = client.request(
                        method=method,
                        url=url,
                        headers=self._headers(),
                        params=params,
                    )

                    if resp.status_code in (401, 403):
                        raise UnityCatalogAuthError(
                            f"Unity Catalog authentication failed ({resp.status_code}) on {path}"
                        )
                    if resp.status_code == 404:
                        raise UnityCatalogNotFoundError(f"Unity Catalog resource not found: {path}")
                    if resp.status_code in (429, 500, 502, 503, 504):
                        if attempt == _MAX_RETRIES - 1:
                            resp.raise_for_status()
                        delay = 1.5 * (2**attempt)
                        logger.warning(
                            "Unity Catalog: received status %d on %s — retrying in %.1fs (%d/%d)",
                            resp.status_code,
                            path,
                            delay,
                            attempt + 1,
                            _MAX_RETRIES,
                        )
                        time.sleep(delay)
                        continue

                    resp.raise_for_status()
                    if resp.content:
                        return resp.json()
                    return {}

            except (httpx.TimeoutException, httpx.NetworkError) as exc:
                if attempt == _MAX_RETRIES - 1:
                    clean_msg = self.redact(str(exc))
                    raise UnityCatalogError(
                        f"Network error communicating with Unity Catalog: {clean_msg}"
                    ) from exc
                delay = 1.5 * (2**attempt)
                logger.warning(
                    "Unity Catalog: connection error on %s — retrying in %.1fs (%d/%d)",
                    path,
                    delay,
                    attempt + 1,
                    _MAX_RETRIES,
                )
                time.sleep(delay)

        raise UnityCatalogError(
            f"Unity Catalog request failed after {_MAX_RETRIES} attempts on {path}"
        )

    def get_catalogs(self) -> list[dict[str, Any]]:
        """List all catalogs in Unity Catalog."""
        data = self.request("GET", "/api/2.1/unity-catalog/catalogs")
        return data.get("catalogs") or []

    def get_catalog(self, catalog_name: str) -> dict[str, Any]:
        """Fetch details for a single catalog."""
        return self.request("GET", f"/api/2.1/unity-catalog/catalogs/{catalog_name}")

    def get_schemas(self, catalog_name: str) -> list[dict[str, Any]]:
        """List schemas within a specific catalog."""
        params = {"catalog_name": catalog_name}
        data = self.request("GET", "/api/2.1/unity-catalog/schemas", params=params)
        return data.get("schemas") or []

    def get_tables(self, catalog_name: str, schema_name: str) -> list[dict[str, Any]]:
        """List tables and views within a catalog schema."""
        params = {"catalog_name": catalog_name, "schema_name": schema_name}
        data = self.request("GET", "/api/2.1/unity-catalog/tables", params=params)
        return data.get("tables") or []

    def get_table(self, full_name: str) -> dict[str, Any]:
        """Fetch table details including schema columns, data format, and properties."""
        return self.request("GET", f"/api/2.1/unity-catalog/tables/{full_name}")

    def get_registered_models(self, catalog_name: str, schema_name: str) -> list[dict[str, Any]]:
        """List registered AI/ML models within a schema."""
        params = {"catalog_name": catalog_name, "schema_name": schema_name}
        try:
            data = self.request("GET", "/api/2.1/unity-catalog/models", params=params)
            return data.get("registered_models") or []
        except (UnityCatalogNotFoundError, UnityCatalogError):
            return []

    def get_volumes(self, catalog_name: str, schema_name: str) -> list[dict[str, Any]]:
        """List storage volumes (unstructured data containers) within a schema."""
        params = {"catalog_name": catalog_name, "schema_name": schema_name}
        try:
            data = self.request("GET", "/api/2.1/unity-catalog/volumes", params=params)
            return data.get("volumes") or []
        except (UnityCatalogNotFoundError, UnityCatalogError):
            return []
