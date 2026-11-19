import urllib.parse
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional
import aiohttp


@dataclass
class NpmPackage:
    name: str
    latest_version: str
    description: str = ""
    license: str = "MIT"
    homepage: str = ""
    repository: str = ""
    readme: str = ""
    dependencies: Dict[str, str] = field(default_factory=dict)
    dev_dependencies: Dict[str, str] = field(default_factory=dict)
    peer_dependencies: Dict[str, str] = field(default_factory=dict)
    keywords: List[str] = field(default_factory=list)

    def to_markdown(self) -> str:
        lines = [
            f"# npm Package: {self.name} (v{self.latest_version})",
            f"- **Registry Link:** https://www.npmjs.com/package/{self.name}",
            f"- **License:** {self.license}",
        ]
        if self.homepage:
            lines.append(f"- **Homepage:** {self.homepage}")
        if self.repository:
            lines.append(f"- **Repository:** {self.repository}")
        if self.description:
            lines.append(f"- **Description:** {self.description}")
        if self.keywords:
            lines.append(f"- **Keywords:** {', '.join(self.keywords)}")

        if self.dependencies:
            lines.append("\n### Production Dependencies:")
            for dep, ver in self.dependencies.items():
                lines.append(f"- `{dep}`: {ver}")

        if self.peer_dependencies:
            lines.append("\n### Peer Dependencies:")
            for pdep, pver in self.peer_dependencies.items():
                lines.append(f"- `{pdep}`: {pver}")

        if self.readme and self.readme.strip():
            lines.append(f"\n### Package Documentation & README:\n{self.readme.strip()}")

        return "\n".join(lines)


class NpmConnector:
    """Cognee community connector for the public npm package registry."""

    def __init__(
        self,
        packages: List[str],
        registry_url: str = "https://registry.npmjs.org",
        include_readme: bool = True,
        session: Optional[aiohttp.ClientSession] = None,
    ):
        self.packages = packages
        self.registry_url = registry_url.rstrip("/")
        self.include_readme = include_readme
        self._session = session
        self._owns_session = False

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()
            self._owns_session = True
        return self._session

    async def close(self) -> None:
        if self._owns_session and self._session and not self._session.closed:
            await self._session.close()

    async def fetch_package(self, package_name: str) -> Optional[NpmPackage]:
        """Fetch package manifest from npm registry. Scoped packages are safely URL-encoded."""
        session = await self._get_session()
        # Handle scoped packages like @angular/core -> @angular%2Fcore
        encoded_pkg = urllib.parse.quote(package_name, safe="@")
        url = f"{self.registry_url}/{encoded_pkg}"

        headers = {
            "Accept": "application/json",
            "User-Agent": "Cognee-Connector-Npm/0.1.0",
        }

        async with session.get(url, headers=headers) as response:
            if response.status == 404:
                # Package does not exist or was unpublished; skip gracefully
                return None
            if response.status == 429:
                raise RuntimeError("npm registry rate limit exceeded (HTTP 429)")
            if response.status >= 400:
                text = await response.text()
                raise RuntimeError(f"npm registry HTTP {response.status} error: {text}")

            data = await response.json()
            if not isinstance(data, dict):
                return None

            dist_tags = data.get("dist-tags", {})
            latest_v = dist_tags.get("latest", "")
            versions = data.get("versions", {})
            v_manifest = versions.get(latest_v, {}) if latest_v else {}

            # Fallback if no dist-tag latest
            if not v_manifest and versions:
                latest_v = list(versions.keys())[-1]
                v_manifest = versions[latest_v]

            # License handling
            raw_license = v_manifest.get("license") or data.get("license", "Unspecified")
            if isinstance(raw_license, dict):
                license_str = raw_license.get("type", "Unspecified")
            else:
                license_str = str(raw_license)

            # Repository handling
            raw_repo = v_manifest.get("repository") or data.get("repository", "")
            if isinstance(raw_repo, dict):
                repo_url = raw_repo.get("url", "")
            else:
                repo_url = str(raw_repo)

            readme_text = ""
            if self.include_readme:
                readme_text = data.get("readme") or v_manifest.get("readme", "")

            return NpmPackage(
                name=data.get("name", package_name),
                latest_version=latest_v or "0.0.0",
                description=v_manifest.get("description") or data.get("description", ""),
                license=license_str,
                homepage=v_manifest.get("homepage") or data.get("homepage", ""),
                repository=repo_url,
                readme=readme_text,
                dependencies=v_manifest.get("dependencies", {}),
                dev_dependencies=v_manifest.get("devDependencies", {}),
                peer_dependencies=v_manifest.get("peerDependencies", {}),
                keywords=v_manifest.get("keywords") or data.get("keywords", []),
            )

    async def fetch_all_packages(self) -> List[NpmPackage]:
        """Fetch all configured packages in sequence."""
        results = []
        for pkg_name in self.packages:
            try:
                pkg = await self.fetch_package(pkg_name)
                if pkg:
                    results.append(pkg)
            except Exception as e:
                # Log or handle transient failure without aborting entire sync
                if "rate limit" in str(e).lower():
                    raise
                continue
        return results

    async def to_documents(self) -> List[str]:
        """Convert all fetched packages to Markdown documents for Cognee memory ingestion."""
        packages = await self.fetch_all_packages()
        return [pkg.to_markdown() for pkg in packages]
