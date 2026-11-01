"""NuGet v3 (.NET package registry) data-source connector for cognee.

Syncs .NET and C# package releases, target-framework dependency groups, upstream
deprecation notices (with author-recommended replacement packages), and vulnerability
advisories (GHSA / CVE) from api.nuget.org into Cognee AI graph & vector memory.

Key Features:
- Dynamic v3 service discovery from https://api.nuget.org/v3/index.json
- Ingests package metadata, authors, license expressions, and project URLs
- Tracks target-framework dependency groups (net8.0, net7.0, netstandard2.0, etc.)
- Ingests upstream Microsoft deprecation metadata (reasons, migration message, alternatePackage)
- Ingests known vulnerability advisories (advisoryUrl, severity)
- Supports extracting dependencies directly from .csproj, Directory.Packages.props, or packages.lock.json
- Unlisted / unlisted-version handling (sentinel published date or listed=False) with tombstoning
- Standardized DLT source with write_disposition="merge" and DOCUMENT_SOURCE_ATTR="nuget"
"""

from __future__ import annotations

import json
import logging
import re
import time
import xml.etree.ElementTree as ET
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

try:
    import dlt
except ImportError:
    dlt = None

try:
    from cognee.tasks.ingestion.dlt_utils import DOCUMENT_SOURCE_ATTR
except ImportError:
    DOCUMENT_SOURCE_ATTR = "cognee_document_source"

logger = logging.getLogger("cognee_connector_nuget")

DEFAULT_NUGET_INDEX_URL = "https://api.nuget.org/v3/index.json"
NUGET_WEB_BASE = "https://www.nuget.org"

_MAX_RETRIES = 3


class NugetConnectorError(RuntimeError):
    """Raised when an unrecoverable NuGet v3 API failure occurs."""

    def __init__(self, message: str, status_code: int = 0):
        super().__init__(message)
        self.status_code = status_code


@dataclass(frozen=True)
class NugetRenderOptions:
    """Configurable rendering options for NuGet documents."""

    include_vulnerabilities: bool = True
    include_dependencies: bool = True
    include_unlisted: bool = False
    exclude_deprecated: bool = False
    max_recent_versions: int = 5


class NugetClient:
    """Client for the NuGet v3 JSON-LD and Registration API."""

    def __init__(
        self,
        service_index_url: str | None = None,
        api_key: str | None = None,
        client: httpx.Client | None = None,
    ):
        self.service_index_url = service_index_url or DEFAULT_NUGET_INDEX_URL
        self.api_key = api_key
        self._client = client or httpx.Client()
        self._owns_client = client is None

        # Discovered endpoints
        self.registration_base_url: str | None = None
        self.search_query_url: str | None = None

    def close(self):
        """Close underlying HTTP client if owned."""
        if self._owns_client:
            self._client.close()

    def _get_headers(self) -> dict[str, str]:
        headers = {
            "Accept": "application/json",
            "User-Agent": "cognee-community-connector-nuget/0.1.0 (Cognee AI Memory; +https://github.com/topoteretes/cognee)",
        }
        if self.api_key:
            headers["X-NuGet-ApiKey"] = self.api_key
        return headers

    def request(self, url: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        """Send authenticated or public request with automatic backoff on 429."""
        params = params or {}

        for attempt in range(1, _MAX_RETRIES + 1):
            try:
                resp = self._client.get(
                    url,
                    headers=self._get_headers(),
                    params=params,
                    timeout=30.0,
                )

                if resp.status_code == 429:
                    retry_after = int(resp.headers.get("Retry-After", "2"))
                    logger.warning("NuGet rate limit encountered, sleeping for %ds", retry_after)
                    time.sleep(retry_after)
                    continue

                if resp.status_code == 404:
                    raise NugetConnectorError(f"NuGet resource not found: {url}", 404)

                if resp.status_code in (401, 403):
                    raise NugetConnectorError(f"Unauthorized/Forbidden on NuGet: {url}", resp.status_code)

                if resp.status_code >= 400:
                    raise NugetConnectorError(f"NuGet API error ({resp.status_code}): {resp.text}", resp.status_code)

                return resp.json()

            except (httpx.NetworkError, httpx.TimeoutException) as exc:
                if attempt == _MAX_RETRIES:
                    raise NugetConnectorError(f"NuGet connection failed after {attempt} attempts: {exc}") from exc
                time.sleep(1.0 * attempt)

        raise NugetConnectorError("Max retries exceeded for NuGet request")

    def discover_endpoints(self) -> None:
        """Resolve service endpoints dynamically from the NuGet v3 index.json."""
        if self.registration_base_url and self.search_query_url:
            return

        try:
            data = self.request(self.service_index_url)
            resources = data.get("resources", [])

            for res in resources:
                res_type = res.get("@type", "")
                res_id = res.get("@id", "")

                # Prefer SemVer2 versioned registration endpoint
                if "RegistrationsBaseUrl/Versioned" in res_type or (
                    "RegistrationsBaseUrl" in res_type and not self.registration_base_url
                ):
                    self.registration_base_url = res_id.rstrip("/")

                if "SearchQueryService" in res_type and not self.search_query_url:
                    self.search_query_url = res_id.rstrip("/")

        except Exception as e:
            logger.warning("Failed discovering NuGet endpoints from %s: %s; using canonical defaults.", self.service_index_url, e)

        # Canonical fallback if index discovery failed
        if not self.registration_base_url:
            self.registration_base_url = "https://api.nuget.org/v3/registration5-gz-semver2"
        if not self.search_query_url:
            self.search_query_url = "https://azuresearch-usnc.nuget.org/query"

    def get_package_registration(self, package_id: str) -> list[dict[str, Any]]:
        """Fetch all catalog entries for a package across its registration pages."""
        self.discover_endpoints()
        pkg_lower = package_id.strip().lower()
        url = f"{self.registration_base_url}/{pkg_lower}/index.json"

        data = self.request(url)
        pages = data.get("items", [])
        catalog_entries = []

        for page in pages:
            # Check if leaves are inline or externalized behind an @id URL
            items = page.get("items")
            if items is None and "@id" in page:
                page_url = page["@id"]
                page_data = self.request(page_url)
                items = page_data.get("items", [])

            for item in items or []:
                entry = item.get("catalogEntry", {})
                if entry:
                    catalog_entries.append(entry)

        return catalog_entries

    def search_packages(self, query: str, limit: int = 10) -> list[str]:
        """Search NuGet for packages matching query string."""
        self.discover_endpoints()
        data = self.request(self.search_query_url, params={"q": query.strip(), "take": limit})
        return [item.get("id") for item in data.get("data", []) if item.get("id")]


def extract_packages_from_dotnet_file(file_path: str | Path) -> list[str]:
    """Extract PackageReference dependencies from .csproj, Directory.Packages.props, or packages.lock.json."""
    path = Path(file_path)
    if not path.is_file():
        raise FileNotFoundError(f".NET project file not found: {path}")

    content = path.read_text(encoding="utf-8")
    packages: set[str] = set()

    # Case 1: packages.lock.json
    if path.name.lower().endswith(".json"):
        try:
            data = json.loads(content)
            # Standard NuGet packages.lock.json has targets -> framework -> dependencies
            targets = data.get("dependencies", {}) or data.get("targets", {})
            for group in targets.values():
                if isinstance(group, dict):
                    for pkg_name in group.keys():
                        packages.add(pkg_name)
        except Exception as e:
            logger.warning("Error parsing JSON packages lock file %s: %s", path, e)

    # Case 2: XML (.csproj, Directory.Packages.props, Directory.Build.props)
    else:
        try:
            # Strip XML namespaces for simplified element matching
            clean_xml = re.sub(r'\sxmlns="[^"]+"', "", content, count=1)
            root = ET.fromstring(clean_xml)
            for elem in root.iter():
                tag = elem.tag.split("}")[-1]
                if tag in ("PackageReference", "PackageVersion"):
                    pkg_id = elem.attrib.get("Include") or elem.attrib.get("Update")
                    if pkg_id:
                        packages.add(pkg_id.strip())
        except Exception:
            # Regex fallback if XML is unparseable or partial
            matches = re.findall(r'<Package(?:Reference|Version)\s+[^>]*Include="([^"]+)"', content, re.IGNORECASE)
            packages.update(matches)

    return sorted(packages)


def render_nuget_document(
    package_id: str,
    entries: list[dict[str, Any]],
    options: NugetRenderOptions = NugetRenderOptions(),
) -> dict[str, Any]:
    """Convert NuGet registration catalog entries into a structured Cognee document."""
    if not entries:
        raise ValueError(f"No catalog entries provided for package '{package_id}'")

    # Latest release is the last entry in the registration index
    latest = entries[-1]
    title = latest.get("title") or package_id
    description = latest.get("description", "No description provided.")
    authors = latest.get("authors", "Unspecified")
    if isinstance(authors, list):
        authors = ", ".join(authors)

    latest_version = latest.get("version", "Unknown")
    project_url = latest.get("projectUrl", "")
    license_expr = latest.get("licenseExpression") or latest.get("licenseUrl", "Unspecified")
    total_downloads = latest.get("totalDownloads", latest.get("downloads", 0))
    web_url = f"{NUGET_WEB_BASE}/packages/{package_id}"

    # Deprecation analysis
    deprecation = latest.get("deprecation")
    is_deprecated = deprecation is not None
    alternate_pkg_id = None
    deprecation_reasons = []
    deprecation_msg = ""

    if is_deprecated and isinstance(deprecation, dict):
        deprecation_reasons = deprecation.get("reasons", [])
        deprecation_msg = deprecation.get("message", "")
        alternate = deprecation.get("alternatePackage", {})
        if isinstance(alternate, dict):
            alternate_pkg_id = alternate.get("id")

    # Vulnerabilities aggregation across versions
    vulnerabilities: list[dict[str, Any]] = []
    seen_adv = set()
    for entry in entries:
        for v in entry.get("vulnerabilities", []):
            adv_url = v.get("advisoryUrl", "")
            if adv_url and adv_url not in seen_adv:
                seen_adv.add(adv_url)
                vulnerabilities.append({
                    "advisoryUrl": adv_url,
                    "severity": v.get("severity", "Unknown"),
                    "affected_version": entry.get("version"),
                })

    # Build document content
    content_lines = [
        f"# NuGet Package: {package_id}",
        f"- Package ID: {package_id}",
        f"- Title: {title}",
        f"- Latest Version: {latest_version}",
        f"- Authors: {authors}",
        f"- License: {license_expr}",
        f"- Project URL: {project_url or 'N/A'}",
        f"- NuGet URL: {web_url}",
        f"- Total Downloads: {total_downloads:,}" if isinstance(total_downloads, int) else f"- Total Downloads: {total_downloads}",
    ]

    if is_deprecated:
        reasons_str = ", ".join(deprecation_reasons) if deprecation_reasons else "Deprecated"
        if alternate_pkg_id:
            content_lines.append(f"- Status: ⚠️ DEPRECATED [{reasons_str}] (Author recommends: '{alternate_pkg_id}')")
        else:
            content_lines.append(f"- Status: ⚠️ DEPRECATED [{reasons_str}]")
        if deprecation_msg:
            content_lines.append(f"- Deprecation Message: {deprecation_msg}")
    else:
        content_lines.append("- Status: Active / Maintained")

    # Dependencies by framework
    if options.include_dependencies:
        dep_groups = latest.get("dependencyGroups", [])
        if dep_groups:
            content_lines.append("\n## Target Framework Dependency Groups:")
            for group in dep_groups:
                tf = group.get("targetFramework", "Any")
                deps = group.get("dependencies", [])
                if deps:
                    dep_str = ", ".join(f"{d.get('id')} ({d.get('range', '*')})" for d in deps[:10])
                    content_lines.append(f"- Framework [{tf}]: {dep_str}")
                else:
                    content_lines.append(f"- Framework [{tf}]: No external dependencies")

    # Vulnerability Advisories
    if options.include_vulnerabilities:
        content_lines.append(f"\n## Security Advisories ({len(vulnerabilities)} known):")
        if vulnerabilities:
            for v in vulnerabilities:
                content_lines.append(f"- [Severity: {v['severity']}] Advisory: {v['advisoryUrl']} (Version affected: {v['affected_version']})")
        else:
            content_lines.append("- No known security advisories on file.")

    full_content = "\n".join(content_lines)

    return {
        "id": f"nuget:{package_id.lower()}",
        "title": f"NuGet: {package_id}",
        "content": full_content,
        "url": web_url,
        "_deleted": False,
        "package_id": package_id,
        "latest_version": latest_version,
        "is_deprecated": is_deprecated,
        "alternate_package": alternate_pkg_id,
        "vulnerability_count": len(vulnerabilities),
    }


def sync_nuget_records(
    client: NugetClient,
    package_ids: list[str],
    options: NugetRenderOptions = NugetRenderOptions(),
) -> Iterator[dict[str, Any]]:
    """Yield all rendered documents for selected .NET packages."""
    for raw_id in package_ids:
        pkg_id = raw_id.strip()
        if not pkg_id:
            continue

        try:
            entries = client.get_package_registration(pkg_id)
        except NugetConnectorError as exc:
            if exc.status_code == 404:
                logger.info("NuGet package '%s' returned 404; emitting deletion tombstone.", pkg_id)
                yield {
                    "id": f"nuget:{pkg_id.lower()}",
                    "title": f"NuGet: {pkg_id} (Deleted)",
                    "content": f"Package {pkg_id} has been deleted or unlisted upstream.",
                    "url": f"{NUGET_WEB_BASE}/packages/{pkg_id}",
                    "_deleted": True,
                    "package_id": pkg_id,
                }
                continue
            logger.error("Failed fetching NuGet package '%s': %s", pkg_id, exc)
            continue
        except Exception as exc:
            logger.error("Unexpected error fetching NuGet package '%s': %s", pkg_id, exc)
            continue

        if not entries:
            continue

        latest = entries[-1]

        # Check listed status (sentinel published date or listed=False)
        is_listed = latest.get("listed", True)
        if latest.get("published", "").startswith("1900-01-01"):
            is_listed = False

        if not is_listed and not options.include_unlisted:
            logger.info("Package '%s' is unlisted; emitting deletion tombstone.", pkg_id)
            yield {
                "id": f"nuget:{pkg_id.lower()}",
                "title": f"NuGet: {pkg_id} (Unlisted)",
                "content": f"Package {pkg_id} is marked unlisted upstream.",
                "url": f"{NUGET_WEB_BASE}/packages/{pkg_id}",
                "_deleted": True,
                "package_id": pkg_id,
            }
            continue

        if options.exclude_deprecated and latest.get("deprecation"):
            logger.info("Excluding deprecated package '%s' per configuration.", pkg_id)
            yield {
                "id": f"nuget:{pkg_id.lower()}",
                "title": f"NuGet: {pkg_id} (Deprecated)",
                "content": f"Package {pkg_id} excluded because it is marked deprecated.",
                "url": f"{NUGET_WEB_BASE}/packages/{pkg_id}",
                "_deleted": True,
                "package_id": pkg_id,
            }
            continue

        yield render_nuget_document(pkg_id, entries, options)


# ─── DLT Source & Fallback Generator ──────────────────────────────────────────


if dlt is not None:

    @dlt.source(name="nuget")
    def nuget_source(
        packages: list[str] | None = None,
        search_query: str | None = None,
        project_file: str | Path | None = None,
        api_key: str | None = None,
        options: NugetRenderOptions = NugetRenderOptions(),
    ):
        """DLT source to sync NuGet packages and vulnerabilities into Cognee AI memory.

        Args:
            packages: List of NuGet package IDs (e.g. ['Newtonsoft.Json', 'Serilog']).
            search_query: Optional search keyword to discover packages dynamically.
            project_file: Optional path to a .csproj, .props, or packages.lock.json file.
            api_key: Optional NuGet API key for private feeds.
            options: NugetRenderOptions.
        """
        client = NugetClient(api_key=api_key)
        target_packages = list(packages or [])

        if project_file:
            try:
                for p in extract_packages_from_dotnet_file(project_file):
                    if p not in target_packages:
                        target_packages.append(p)
            except Exception as e:
                logger.error("Failed extracting dependencies from %s: %s", project_file, e)

        if search_query:
            try:
                for p in client.search_packages(search_query, limit=5):
                    if p not in target_packages:
                        target_packages.append(p)
            except Exception as e:
                logger.warning("Failed searching NuGet packages for '%s': %s", search_query, e)

        if not target_packages:
            target_packages = ["Newtonsoft.Json", "Microsoft.Extensions.Logging"]

        @dlt.resource(name="nuget_packages", primary_key="id", write_disposition="merge")
        def nuget_packages():
            yield from sync_nuget_records(client, target_packages, options=options)

        setattr(nuget_packages, DOCUMENT_SOURCE_ATTR, "nuget")
        return [nuget_packages]

else:

    def nuget_source(
        packages: list[str] | None = None,
        search_query: str | None = None,
        project_file: str | Path | None = None,
        api_key: str | None = None,
        options: NugetRenderOptions = NugetRenderOptions(),
    ):
        """Fallback generator when dlt is not loaded."""
        client = NugetClient(api_key=api_key)
        target_packages = list(packages or [])

        if project_file:
            try:
                for p in extract_packages_from_dotnet_file(project_file):
                    if p not in target_packages:
                        target_packages.append(p)
            except Exception:
                pass

        if search_query:
            try:
                for p in client.search_packages(search_query, limit=5):
                    if p not in target_packages:
                        target_packages.append(p)
            except Exception:
                pass

        if not target_packages:
            target_packages = ["Newtonsoft.Json", "Microsoft.Extensions.Logging"]

        return list(sync_nuget_records(client, target_packages, options=options))
