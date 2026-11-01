"""HTTP client for querying the official Hex.pm BEAM package registry API."""

import logging
import re
from pathlib import Path
from typing import Any

import httpx

from cognee_community_connector_hexpm.models import (
    HexDependency,
    HexPackage,
    HexReleaseDetail,
    HexRetirement,
)

logger = logging.getLogger(__name__)

DEFAULT_BASE_URL = "https://hex.pm/api"
DEFAULT_USER_AGENT = "cognee-connector-hexpm/0.1.0 (https://github.com/topoteretes/cognee)"


class HexApiError(Exception):
    """Base exception for Hex.pm API errors."""


class HexPackageNotFoundError(HexApiError):
    """Raised when a requested package is missing on Hex.pm."""


class HexRateLimitError(HexApiError):
    """Raised when Hex.pm rate limits are exceeded (HTTP 429)."""


class HexClient:
    """Async HTTP client interacting with the Hex.pm REST API."""

    def __init__(
        self,
        base_url: str = DEFAULT_BASE_URL,
        api_key: str | None = None,
        repository: str | None = None,
        user_agent: str = DEFAULT_USER_AGENT,
        transport: httpx.AsyncBaseTransport | None = None,
        timeout: float = 20.0,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.repository = repository
        self.user_agent = user_agent
        self.transport = transport
        self.timeout = timeout

    def __repr__(self) -> str:
        key_repr = "'***'" if self.api_key else "None"
        return f"HexClient(base_url={self.base_url!r}, api_key={key_repr})"

    def _create_client(self) -> httpx.AsyncClient:
        headers = {
            "User-Agent": self.user_agent,
            "Accept": "application/json",
        }
        if self.api_key:
            headers["Authorization"] = self.api_key

        return httpx.AsyncClient(
            base_url=self.base_url,
            headers=headers,
            transport=self.transport,
            timeout=self.timeout,
        )

    def _build_package_path(self, package_name: str) -> str:
        clean = package_name.strip().lower()
        if self.repository:
            return f"/repos/{self.repository}/packages/{clean}"
        return f"/packages/{clean}"

    async def get_package(
        self, package_name: str, etag: str | None = None
    ) -> tuple[dict[str, Any] | None, str | None]:
        """Fetch package manifest from Hex.pm, supporting ETag conditional requests."""
        clean = package_name.strip().lower()
        if not clean:
            raise ValueError("Package name cannot be empty")

        path = self._build_package_path(clean)
        headers = {}
        if etag:
            headers["If-None-Match"] = etag

        async with self._create_client() as client:
            resp = await client.get(path, headers=headers)
            if resp.status_code == 304:
                return None, etag
            if resp.status_code == 404:
                raise HexPackageNotFoundError(f"Package '{clean}' not found on Hex.pm")
            if resp.status_code == 429:
                reset = resp.headers.get("X-RateLimit-Reset", "60")
                raise HexRateLimitError(f"Hex.pm rate limit reached. Reset in {reset}s")
            resp.raise_for_status()

            new_etag = resp.headers.get("ETag")
            return resp.json(), new_etag

    async def get_release(self, package_name: str, version: str) -> dict[str, Any]:
        """Fetch release details including dependencies and retirement notices."""
        clean = package_name.strip().lower()
        if self.repository:
            path = f"/repos/{self.repository}/packages/{clean}/releases/{version}"
        else:
            path = f"/packages/{clean}/releases/{version}"

        async with self._create_client() as client:
            resp = await client.get(path)
            if resp.status_code == 404:
                return {}
            if resp.status_code == 429:
                raise HexRateLimitError("Hex.pm rate limit reached while reading release")
            resp.raise_for_status()
            return resp.json()

    async def search_packages(self, query: str, sort: str = "updated_at") -> list[dict[str, Any]]:
        """Search Hex.pm for packages matching query string."""
        path = "/packages"
        params = {"search": query, "sort": sort}
        async with self._create_client() as client:
            resp = await client.get(path, params=params)
            resp.raise_for_status()
            return resp.json()

    async def fetch_package_entity(
        self, package_name: str, version: str | None = None
    ) -> HexPackage:
        """Fetch complete package with latest release requirements and retirements."""
        data, _ = await self.get_package(package_name)
        if not data:
            raise HexPackageNotFoundError(f"Unable to read package '{package_name}'")

        name = data.get("name", package_name)
        meta = data.get("meta", {})
        releases = data.get("releases", [])

        # Parse top-level retirements map
        raw_retirements = data.get("retirements", {})
        all_retirements: dict[str, HexRetirement] = {}
        if isinstance(raw_retirements, dict):
            for v_key, r_info in raw_retirements.items():
                if isinstance(r_info, dict):
                    all_retirements[v_key] = HexRetirement(
                        version=v_key,
                        reason=r_info.get("reason", "invalid"),
                        message=r_info.get("message"),
                    )

        # Identify target version (user requested, or latest from releases[0])
        target_version = version
        if not target_version and releases:
            target_version = releases[0].get("version", "0.1.0")

        # Fetch release details if target version identified
        release_detail: HexReleaseDetail | None = None
        if target_version:
            rel_data = await self.get_release(name, target_version)
            if rel_data:
                reqs_raw = rel_data.get("requirements", {})
                requirements: list[HexDependency] = []
                if isinstance(reqs_raw, dict):
                    for req_key, req_val in reqs_raw.items():
                        requirements.append(
                            HexDependency(
                                name=req_key,
                                app=req_val.get("app", req_key),
                                requirement=req_val.get("requirement", "*"),
                                optional=req_val.get("optional", False),
                            )
                        )

                ret_notice = all_retirements.get(target_version)
                if not ret_notice and rel_data.get("retirement"):
                    r = rel_data["retirement"]
                    ret_notice = HexRetirement(
                        version=target_version,
                        reason=r.get("reason", "invalid"),
                        message=r.get("message"),
                    )

                release_detail = HexReleaseDetail(
                    version=target_version,
                    has_docs=rel_data.get("has_docs", False),
                    docs_html_url=rel_data.get("docs_html_url"),
                    checksum=rel_data.get("checksum"),
                    requirements=requirements,
                    retirement=ret_notice,
                    security_advisories=rel_data.get("security_advisories", []),
                    downloads=rel_data.get("downloads", 0),
                    inserted_at=rel_data.get("inserted_at"),
                    updated_at=rel_data.get("updated_at"),
                )

        return HexPackage(
            name=name,
            description=meta.get("description", ""),
            licenses=meta.get("licenses", []),
            links=meta.get("links", {}),
            maintainers=meta.get("maintainers", []),
            latest_version=target_version or "0.1.0",
            latest_release=release_detail,
            all_retirements=all_retirements,
            downloads=data.get("downloads", {}),
            html_url=data.get("html_url", f"https://hex.pm/packages/{name}"),
            docs_html_url=data.get("docs_html_url", f"https://hexdocs.pm/{name}"),
            inserted_at=data.get("inserted_at"),
            updated_at=data.get("updated_at"),
        )

    async def fetch_packages_batch(self, package_names: list[str]) -> list[HexPackage]:
        """Fetch multiple packages safely, skipping nonexistent entries."""
        entities: list[HexPackage] = []
        for name in package_names:
            try:
                pkg = await self.fetch_package_entity(name)
                entities.append(pkg)
            except HexPackageNotFoundError:
                logger.warning("Hex package '%s' not found, skipping", name)
            except Exception as e:
                logger.error("Error fetching hex package '%s': %s", name, e)
        return entities

    @staticmethod
    def parse_mix_lock(mix_lock_path: str | Path) -> list[str]:
        """Parse Elixir mix.lock file to extract configured Hex packages."""
        path = Path(mix_lock_path)
        if not path.is_file():
            raise FileNotFoundError(f"mix.lock file not found at '{path}'")

        content = path.read_text(encoding="utf-8")
        # Match pattern: :hex, :package_name
        matches = re.findall(r":hex,\s*:([a-zA-Z0-9_]+)", content)
        return list(dict.fromkeys(matches))
