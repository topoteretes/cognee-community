"""HTTP client for querying Crates.io Rust package registry API."""

import logging
import tomllib
from pathlib import Path
from typing import Any

import httpx

from cognee_community_connector_crates.models import (
    CrateDependency,
    CrateMetadata,
    CratePackage,
    CrateVersion,
)

logger = logging.getLogger(__name__)

DEFAULT_BASE_URL = "https://crates.io/api/v1"
DEFAULT_USER_AGENT = "cognee-connector-crates/0.1.0 (https://github.com/topoteretes/cognee)"


class CratesApiError(Exception):
    """Base exception for Crates.io API errors."""


class CrateNotFoundError(CratesApiError):
    """Raised when a requested crate does not exist on crates.io."""


class CratesRateLimitError(CratesApiError):
    """Raised when crates.io rate limit (HTTP 429) is encountered."""


class CratesClient:
    """Async client interacting with the public Crates.io API."""

    def __init__(
        self,
        base_url: str = DEFAULT_BASE_URL,
        user_agent: str = DEFAULT_USER_AGENT,
        transport: httpx.AsyncBaseTransport | None = None,
        timeout: float = 20.0,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.user_agent = user_agent
        self.transport = transport
        self.timeout = timeout

    def __repr__(self) -> str:
        return f"CratesClient(base_url={self.base_url!r}, user_agent={self.user_agent!r})"

    def _create_client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            base_url=self.base_url,
            headers={
                "User-Agent": self.user_agent,
                "Accept": "application/json",
            },
            transport=self.transport,
            timeout=self.timeout,
        )

    async def get_crate(self, crate_name: str) -> dict[str, Any]:
        """Fetch primary crate manifest and versions list."""
        clean_name = crate_name.strip().lower()
        if not clean_name:
            raise ValueError("Crate name cannot be empty")

        url = f"/crates/{clean_name}"
        async with self._create_client() as client:
            resp = await client.get(url)
            if resp.status_code == 404:
                raise CrateNotFoundError(f"Crate '{clean_name}' not found on crates.io")
            if resp.status_code == 429:
                retry_after = resp.headers.get("Retry-After", "60")
                raise CratesRateLimitError(
                    f"Crates.io rate limit exceeded. Retry after {retry_after}s"
                )
            resp.raise_for_status()
            return resp.json()

    async def get_dependencies(self, crate_name: str, version: str) -> list[dict[str, Any]]:
        """Fetch dependencies for a specific crate release."""
        clean_name = crate_name.strip().lower()
        url = f"/crates/{clean_name}/{version}/dependencies"
        async with self._create_client() as client:
            resp = await client.get(url)
            if resp.status_code == 404:
                return []
            if resp.status_code == 429:
                raise CratesRateLimitError(
                    "Crates.io rate limit exceeded while reading dependencies"
                )
            resp.raise_for_status()
            data = resp.json()
            return data.get("dependencies", [])

    async def get_readme(self, crate_name: str, version: str) -> str | None:
        """Fetch documentation / README text for a specific release."""
        clean_name = crate_name.strip().lower()
        url = f"/crates/{clean_name}/{version}/readme"
        async with self._create_client() as client:
            resp = await client.get(url)
            if resp.status_code == 404:
                return None
            if resp.status_code == 429:
                raise CratesRateLimitError("Crates.io rate limit exceeded while reading readme")
            if resp.is_success:
                return resp.text
            return None

    async def search_crates(self, query: str, limit: int = 10) -> list[dict[str, Any]]:
        """Search crates.io for packages matching query string."""
        url = "/crates"
        params: dict[str, str | int] = {"q": query, "per_page": min(limit, 50)}
        async with self._create_client() as client:
            resp = await client.get(url, params=params)
            resp.raise_for_status()
            data = resp.json()
            return data.get("crates", [])

    async def fetch_crate_package(
        self, crate_name: str, version: str | None = None
    ) -> CratePackage:
        """Fetch complete package with version metadata, dependencies, and README."""
        data = await self.get_crate(crate_name)
        crate_info = data.get("crate", {})
        versions_raw = data.get("versions", [])

        # Locate chosen version or latest stable
        target_version_dict: dict[str, Any] | None = None
        if version:
            for v in versions_raw:
                if v.get("num") == version:
                    target_version_dict = v
                    break
        elif versions_raw:
            target_version_dict = versions_raw[0]

        ver_num = (
            target_version_dict.get("num", crate_info.get("max_version", "0.1.0"))
            if target_version_dict
            else crate_info.get("max_version", "0.1.0")
        )

        metadata = CrateMetadata(
            name=crate_info.get("name", crate_name),
            description=crate_info.get("description", ""),
            max_version=crate_info.get("max_version", ver_num),
            documentation=crate_info.get("documentation"),
            repository=crate_info.get("repository"),
            homepage=crate_info.get("homepage"),
            downloads=crate_info.get("downloads", 0),
            recent_downloads=crate_info.get("recent_downloads"),
            keywords=data.get("keywords", []) if isinstance(data.get("keywords"), list) else [],
            categories=data.get("categories", [])
            if isinstance(data.get("categories"), list)
            else [],
            updated_at=crate_info.get("updated_at"),
        )

        version_model: CrateVersion | None = None
        if target_version_dict:
            raw_features = target_version_dict.get("features", {})
            parsed_features = (
                {k: list(v) for k, v in raw_features.items()}
                if isinstance(raw_features, dict)
                else {}
            )
            version_model = CrateVersion(
                num=ver_num,
                license=target_version_dict.get("license"),
                downloads=target_version_dict.get("downloads", 0),
                features=parsed_features,
                yanked=target_version_dict.get("yanked", False),
                created_at=target_version_dict.get("created_at"),
                updated_at=target_version_dict.get("updated_at"),
            )

        # Ingest dependencies and README
        raw_deps = await self.get_dependencies(crate_name, ver_num)
        dependencies: list[CrateDependency] = []
        for d in raw_deps:
            dependencies.append(
                CrateDependency(
                    crate_id=d.get("crate_id", ""),
                    req=d.get("req", "*"),
                    kind=d.get("kind", "normal"),
                    optional=d.get("optional", False),
                    features=d.get("features", []),
                    target=d.get("target"),
                )
            )

        readme_text = await self.get_readme(crate_name, ver_num)

        return CratePackage(
            metadata=metadata,
            latest_version=version_model,
            dependencies=dependencies,
            readme=readme_text,
        )

    async def fetch_packages_batch(self, crate_names: list[str]) -> list[CratePackage]:
        """Fetch multiple crates, skipping non-existent ones safely."""
        packages: list[CratePackage] = []
        for name in crate_names:
            try:
                pkg = await self.fetch_crate_package(name)
                packages.append(pkg)
            except CrateNotFoundError:
                logger.warning("Crate '%s' not found on crates.io, skipping", name)
            except Exception as e:
                logger.error("Failed to fetch crate '%s': %s", name, e)
        return packages

    @staticmethod
    def parse_cargo_toml(
        cargo_toml_path: str | Path,
        include_dev: bool = False,
        include_build: bool = False,
    ) -> list[str]:
        """Parse local Cargo.toml to extract crate dependencies."""
        path = Path(cargo_toml_path)
        if not path.is_file():
            raise FileNotFoundError(f"Cargo.toml not found at '{path}'")

        with open(path, "rb") as f:
            data = tomllib.load(f)

        crates: list[str] = []
        deps = data.get("dependencies", {})
        if isinstance(deps, dict):
            crates.extend(deps.keys())

        if include_dev:
            dev_deps = data.get("dev-dependencies", {})
            if isinstance(dev_deps, dict):
                crates.extend(dev_deps.keys())

        if include_build:
            build_deps = data.get("build-dependencies", {})
            if isinstance(build_deps, dict):
                crates.extend(build_deps.keys())

        # Deduplicate while preserving order
        return list(dict.fromkeys(crates))
