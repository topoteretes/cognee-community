"""JFrog Artifactory REST API client for repos, artifacts, and CI/CD builds."""

import os
import time
from typing import Any
from urllib.parse import urlparse

import httpx

try:
    from cognee.shared.logging_utils import get_logger

    logger = get_logger("artifactory_client")
except ImportError:
    import logging

    logger = logging.getLogger("artifactory_client")

_MAX_RETRIES = 3
_DEFAULT_TIMEOUT = 30.0


class ArtifactoryError(Exception):
    """Base exception for JFrog Artifactory connector errors."""


class ArtifactoryAuthError(ArtifactoryError):
    """Authentication or authorization failure against JFrog Artifactory API."""


class ArtifactoryNotFoundError(ArtifactoryError):
    """Requested repository, artifact, or build not found in JFrog Artifactory."""


class ArtifactoryClient:
    """Client for interacting with the JFrog Artifactory REST API."""

    def __init__(
        self,
        base_url: str | None = None,
        token: str | None = None,
        api_key: str | None = None,
        username: str | None = None,
        password: str | None = None,
        verify_ssl: bool = True,
        timeout: float = _DEFAULT_TIMEOUT,
    ):
        raw_url = base_url or os.environ.get("ARTIFACTORY_URL") or os.environ.get("JFROG_URL")
        if not raw_url:
            raise ValueError(
                "JFrog Artifactory URL required: pass base_url= or set ARTIFACTORY_URL."
            )

        parsed = urlparse(raw_url)
        if parsed.scheme not in ("http", "https") or not parsed.netloc:
            raise ValueError(
                f"Invalid JFrog Artifactory URL '{raw_url}'. Scheme and host required."
            )

        # Normalize base URL so it points to the Artifactory API root
        cleaned = raw_url.rstrip("/")
        if not cleaned.endswith("/artifactory") and "/artifactory" not in cleaned:
            cleaned = f"{cleaned}/artifactory"
        self.base_url = cleaned

        self.token = (
            token or os.environ.get("ARTIFACTORY_TOKEN") or os.environ.get("JFROG_ACCESS_TOKEN")
        )
        self.api_key = api_key or os.environ.get("ARTIFACTORY_API_KEY")
        self.username = username or os.environ.get("ARTIFACTORY_USERNAME")
        self.password = password or os.environ.get("ARTIFACTORY_PASSWORD")

        if not any([self.token, self.api_key, (self.username and self.password)]):
            raise ValueError(
                "JFrog Artifactory authentication required: pass token, api_key, or user/pass."
            )

        self.verify_ssl = verify_ssl
        self.timeout = timeout

    def _headers(self) -> dict[str, str]:
        headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
        }
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        elif self.api_key:
            headers["X-JFrog-Art-Api"] = self.api_key
        return headers

    def _auth(self) -> tuple[str, str] | None:
        if not self.token and not self.api_key and self.username and self.password:
            return (self.username, self.password)
        return None

    def redact(self, text: str) -> str:
        """Scrub secret credentials from strings and error messages."""
        if not text:
            return text
        secrets = [s for s in [self.token, self.api_key, self.password] if s]
        result = text
        for secret in secrets:
            result = result.replace(secret, "[REDACTED]")
        return result

    def request(
        self,
        method: str,
        path: str,
        params: dict[str, Any] | None = None,
        data: Any = None,
        headers: dict[str, str] | None = None,
    ) -> Any:
        """Execute HTTP request with exponential backoff on transient errors."""
        endpoint = path.lstrip("/")
        url = f"{self.base_url}/{endpoint}"

        req_headers = self._headers()
        if headers:
            req_headers.update(headers)

        for attempt in range(_MAX_RETRIES):
            try:
                with httpx.Client(verify=self.verify_ssl, timeout=self.timeout) as client:
                    resp = client.request(
                        method=method,
                        url=url,
                        headers=req_headers,
                        params=params,
                        content=data if isinstance(data, (bytes, str)) else None,
                        json=data if isinstance(data, dict) else None,
                        auth=self._auth(),
                    )

                    if resp.status_code in (401, 403):
                        raise ArtifactoryAuthError(
                            f"Artifactory auth failed ({resp.status_code}) on {path}"
                        )
                    if resp.status_code == 404:
                        raise ArtifactoryNotFoundError(
                            f"JFrog Artifactory resource not found: {path}"
                        )
                    if resp.status_code in (429, 500, 502, 503, 504):
                        if attempt == _MAX_RETRIES - 1:
                            resp.raise_for_status()
                        delay = 1.5 * (2**attempt)
                        logger.warning(
                            "Artifactory: received status %d on %s — retrying in %.1fs (%d/%d)",
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
                    raise ArtifactoryError(
                        f"Network error communicating with Artifactory: {clean_msg}"
                    ) from exc
                delay = 1.5 * (2**attempt)
                logger.warning(
                    "Artifactory: connection error on %s — retrying in %.1fs (%d/%d)",
                    path,
                    delay,
                    attempt + 1,
                    _MAX_RETRIES,
                )
                time.sleep(delay)

    def get_repositories(
        self,
        repo_type: str | None = None,
        package_type: str | None = None,
    ) -> list[dict[str, Any]]:
        """List repositories in Artifactory, optionally filtered by type."""
        params: dict[str, Any] = {}
        if repo_type:
            params["type"] = repo_type
        if package_type:
            params["packageType"] = package_type

        data = self.request("GET", "/api/repositories", params=params)
        if isinstance(data, list):
            return data
        return []

    def get_repository_details(self, repo_key: str) -> dict[str, Any]:
        """Fetch configuration and storage settings for a specific repository."""
        return self.request("GET", f"/api/repositories/{repo_key}")

    def get_item_info(self, repo_key: str, path: str = "") -> dict[str, Any]:
        """Fetch item metadata (file or directory) from the storage API."""
        clean_path = path.lstrip("/")
        endpoint = (
            f"/api/storage/{repo_key}/{clean_path}" if clean_path else f"/api/storage/{repo_key}"
        )
        return self.request("GET", endpoint)

    def get_item_properties(self, repo_key: str, path: str = "") -> dict[str, Any]:
        """Fetch custom user properties attached to an artifact (e.g. build info, git commit)."""
        clean_path = path.lstrip("/")
        endpoint = (
            f"/api/storage/{repo_key}/{clean_path}" if clean_path else f"/api/storage/{repo_key}"
        )
        try:
            data = self.request("GET", endpoint, params={"properties": ""})
            return data.get("properties") or {}
        except (ArtifactoryNotFoundError, ArtifactoryError):
            return {}

    def get_build_runs(self, build_name: str | None = None) -> list[dict[str, Any]]:
        """List build runs or build names registered in Artifactory."""
        if build_name:
            endpoint = f"/api/build/{build_name}"
            data = self.request("GET", endpoint)
            builds = data.get("buildsNumbers") or []
            return [
                {
                    "build_name": build_name,
                    "uri": b.get("uri", "").lstrip("/"),
                    "started": b.get("started"),
                }
                for b in builds
            ]

        data = self.request("GET", "/api/build")
        builds = data.get("builds") or []
        return [
            {"build_name": b.get("uri", "").lstrip("/"), "uri": b.get("uri", "")} for b in builds
        ]

    def get_build_info(self, build_name: str, build_number: str) -> dict[str, Any]:
        """Fetch comprehensive build info (modules, dependencies, CI agent, git VCS) for a run."""
        endpoint = f"/api/build/{build_name}/{build_number}"
        data = self.request("GET", endpoint)
        return data.get("buildInfo") or data

    def execute_aql(self, query: str) -> list[dict[str, Any]]:
        """Execute an Artifactory Query Language (AQL) search query."""
        headers = {"Content-Type": "text/plain"}
        data = self.request("POST", "/api/search/aql", data=query, headers=headers)
        return data.get("results") or []
