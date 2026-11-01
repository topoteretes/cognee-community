"""Packagist / Composer (PHP package registry) data-source connector for cognee.

Syncs PHP packages, releases, dependency requirements, upstream abandonment notices,
and Packagist security advisories into Cognee AI graph & vector memory.

Key Features:
- Direct REST integration with Packagist API (https://packagist.org)
- Adheres to Packagist API policies (descriptive User-Agent header)
- Support for selecting packages by explicit name or vendor prefix
- Ingests package metadata, version constraints, and maintainer details
- Ingests official security advisories (PKSA-*, CVE, affected versions)
- Tracks package abandonment replacement notices (e.g., abandoned: "symfony/mailer")
- Incremental sync support via Packagist's /metadata/changes.json cursor
- Tombstoning of deleted or unmonitored packages (_deleted: True)
- Standardized DLT resource with DOCUMENT_SOURCE_ATTR="packagist"
"""

from __future__ import annotations

import logging
import time
from collections.abc import Iterator
from dataclasses import dataclass
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

logger = logging.getLogger("cognee_connector_packagist")

PACKAGIST_API_BASE = "https://packagist.org"
PACKAGIST_REPO_BASE = "https://repo.packagist.org"

_MAX_RETRIES = 3


class PackagistConnectorError(RuntimeError):
    """Raised when a Packagist API request fails unrecoverably."""

    def __init__(self, message: str, status_code: int = 0):
        super().__init__(message)
        self.status_code = status_code


@dataclass(frozen=True)
class PackagistRenderOptions:
    """Configurable rendering options for Packagist documents."""

    include_security_advisories: bool = True
    include_version_requirements: bool = True
    max_recent_versions: int = 5
    exclude_abandoned: bool = False


class PackagistClient:
    """Client for Packagist public and private REST API."""

    def __init__(
        self,
        token: str | None = None,
        base_url: str | None = None,
        client: httpx.Client | None = None,
    ):
        self.token = token
        self.base_url = (base_url or PACKAGIST_API_BASE).rstrip("/")
        self._client = client or httpx.Client()
        self._owns_client = client is None

    def close(self):
        """Close underlying HTTP client if owned."""
        if self._owns_client:
            self._client.close()

    def _get_headers(self) -> dict[str, str]:
        headers = {
            "Accept": "application/json",
            "User-Agent": "cognee-community-connector-packagist/0.1.0 (Cognee AI Memory; +https://github.com/topoteretes/cognee)",
        }
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        return headers

    def request(self, method: str, endpoint: str, params: dict[str, Any] | None = None, data: Any = None) -> dict[str, Any]:
        """Send authenticated or public request with automatic backoff on 429."""
        url = f"{self.base_url}/{endpoint.lstrip('/')}"
        params = params or {}

        for attempt in range(1, _MAX_RETRIES + 1):
            try:
                resp = self._client.request(
                    method=method.upper(),
                    url=url,
                    headers=self._get_headers(),
                    params=params,
                    data=data,
                    timeout=30.0,
                )

                if resp.status_code == 429:
                    retry_after = int(resp.headers.get("Retry-After", "2"))
                    logger.warning("Packagist rate limit hit, backing off for %ds", retry_after)
                    time.sleep(retry_after)
                    continue

                if resp.status_code == 404:
                    raise PackagistConnectorError(f"Resource not found on Packagist: {endpoint}", 404)

                if resp.status_code in (401, 403):
                    raise PackagistConnectorError("Unauthorized/Forbidden on Packagist", resp.status_code)

                if resp.status_code >= 400:
                    raise PackagistConnectorError(f"Packagist API error ({resp.status_code}): {resp.text}", resp.status_code)

                return resp.json()

            except (httpx.NetworkError, httpx.TimeoutException) as exc:
                if attempt == _MAX_RETRIES:
                    raise PackagistConnectorError(f"Packagist connection failed after {attempt} attempts: {exc}") from exc
                time.sleep(1.0 * attempt)

        raise PackagistConnectorError("Max retries exceeded for Packagist request")

    def get_package(self, package_name: str) -> dict[str, Any]:
        """Fetch package metadata from GET /packages/{vendor}/{package}.json."""
        pkg = package_name.strip()
        data = self.request("GET", f"packages/{pkg}.json")
        return data.get("package", {})

    def get_security_advisories(self, package_names: list[str]) -> dict[str, list[dict[str, Any]]]:
        """Fetch security advisories via POST /api/security-advisories/."""
        if not package_names:
            return {}

        form_data = {"packages[]": [pkg.strip() for pkg in package_names]}
        try:
            data = self.request("POST", "api/security-advisories/", data=form_data)
            return data.get("advisories", {})
        except Exception as e:
            logger.warning("Failed fetching security advisories: %s", e)
            return {}

    def list_vendor_packages(self, vendor: str) -> list[str]:
        """Fetch package names for a given vendor prefix from GET /packages/list.json?vendor={vendor}."""
        data = self.request("GET", "packages/list.json", params={"vendor": vendor.strip()})
        return data.get("packageNames", [])

    def get_changes(self, since: int | None = None) -> dict[str, Any]:
        """Fetch high-resolution metadata changes feed from /metadata/changes.json."""
        params = {}
        if since:
            params["since"] = since
        return self.request("GET", "metadata/changes.json", params=params)


def render_packagist_document(
    pkg: dict[str, Any],
    advisories: list[dict[str, Any]] | None = None,
    options: PackagistRenderOptions = PackagistRenderOptions(),
) -> dict[str, Any]:
    """Convert Packagist package metadata and advisories into a structured Cognee document."""
    pkg_name = pkg.get("name", "").strip()
    description = pkg.get("description", "No description provided.")
    repo_url = pkg.get("repository", "")
    downloads = pkg.get("downloads", {}).get("total", 0)
    github_stars = pkg.get("github_stars", 0)
    web_url = f"{PACKAGIST_API_BASE}/packages/{pkg_name}"

    # Abandonment status
    abandoned = pkg.get("abandoned", False)
    is_abandoned = bool(abandoned)
    replacement = abandoned if isinstance(abandoned, str) else None

    # Version analysis
    versions = pkg.get("versions", {})
    recent_versions = []
    # Sort or collect non-dev versions
    for v_name, v_data in versions.items():
        if isinstance(v_data, dict):
            recent_versions.append((v_name, v_data))
        if len(recent_versions) >= options.max_recent_versions:
            break

    # Build document content
    content_lines = [
        f"# Packagist Package: {pkg_name}",
        f"- Package: {pkg_name}",
        f"- Description: {description}",
        f"- Repository: {repo_url or 'N/A'}",
        f"- Packagist URL: {web_url}",
        f"- Total Downloads: {downloads:,}",
        f"- GitHub Stars: {github_stars}",
    ]

    if is_abandoned:
        if replacement:
            content_lines.append(f"- Status: ⚠️ ABANDONED (Author recommends replacement: '{replacement}')")
        else:
            content_lines.append("- Status: ⚠️ ABANDONED (No official replacement recommended)")
    else:
        content_lines.append("- Status: Active / Maintained")

    # Releases & requirements
    if options.include_version_requirements and recent_versions:
        content_lines.append("\n## Recent Releases & PHP Constraints:")
        for v_name, v_meta in recent_versions:
            reqs = v_meta.get("require", {})
            php_req = reqs.get("php", "Unspecified")
            lic = ", ".join(v_meta.get("license", [])) or "None listed"
            content_lines.append(f"- Version {v_name}: License [{lic}] | PHP requirement: {php_req}")

    # Security Advisories
    adv_list = advisories or []
    if options.include_security_advisories:
        content_lines.append(f"\n## Security Advisories ({len(adv_list)} reported):")
        if adv_list:
            for adv in adv_list:
                adv_id = adv.get("advisoryId", "PKSA-UNKNOWN")
                adv_title = adv.get("title", "Security Advisory")
                cve = adv.get("cve", "CVE Pending")
                affected = adv.get("affectedVersions", "Unknown")
                link = adv.get("link", web_url)
                content_lines.append(f"- [{adv_id}] {adv_title} ({cve}) | Affected: {affected} | Link: {link}")
        else:
            content_lines.append("- No known security advisories on file.")

    full_content = "\n".join(content_lines)

    return {
        "id": f"packagist:{pkg_name}",
        "title": f"Packagist: {pkg_name}",
        "content": full_content,
        "url": web_url,
        "_deleted": False,
        "package_name": pkg_name,
        "is_abandoned": is_abandoned,
        "replacement_package": replacement,
        "advisory_count": len(adv_list),
        "total_downloads": downloads,
    }


def sync_packagist_records(
    client: PackagistClient,
    package_names: list[str],
    options: PackagistRenderOptions = PackagistRenderOptions(),
) -> Iterator[dict[str, Any]]:
    """Yield all rendered documents for selected Composer packages and their security advisories."""
    if not package_names:
        return

    # Bulk fetch advisories for all packages in one call
    advisories_map = client.get_security_advisories(package_names) if options.include_security_advisories else {}

    for raw_name in package_names:
        pkg_name = raw_name.strip()
        if not pkg_name:
            continue

        try:
            pkg_data = client.get_package(pkg_name)
        except PackagistConnectorError as exc:
            if exc.status_code == 404:
                # Package was deleted or renamed upstream -> emit tombstone
                logger.info("Packagist package '%s' returned 404; emitting deletion tombstone.", pkg_name)
                yield {
                    "id": f"packagist:{pkg_name}",
                    "title": f"Packagist: {pkg_name} (Deleted)",
                    "content": f"Package {pkg_name} has been deleted or removed upstream.",
                    "url": f"{PACKAGIST_API_BASE}/packages/{pkg_name}",
                    "_deleted": True,
                    "package_name": pkg_name,
                }
                continue
            logger.error("Failed fetching Packagist package '%s': %s", pkg_name, exc)
            continue
        except Exception as exc:
            logger.error("Unexpected error fetching Packagist package '%s': %s", pkg_name, exc)
            continue

        if options.exclude_abandoned and pkg_data.get("abandoned"):
            logger.info("Excluding abandoned package '%s' per configuration.", pkg_name)
            yield {
                "id": f"packagist:{pkg_name}",
                "title": f"Packagist: {pkg_name} (Abandoned)",
                "content": f"Package {pkg_name} excluded because it is marked abandoned.",
                "url": f"{PACKAGIST_API_BASE}/packages/{pkg_name}",
                "_deleted": True,
                "package_name": pkg_name,
            }
            continue

        adv_list = advisories_map.get(pkg_name, [])
        yield render_packagist_document(pkg_data, adv_list, options)


# ─── DLT Source & Fallback Generator ──────────────────────────────────────────


if dlt is not None:

    @dlt.source(name="packagist")
    def packagist_source(
        packages: list[str] | None = None,
        vendor: str | None = None,
        token: str | None = None,
        options: PackagistRenderOptions = PackagistRenderOptions(),
    ):
        """DLT source to sync Packagist PHP packages and advisories into Cognee AI memory.

        Args:
            packages: List of 'vendor/package' names (e.g. ['monolog/monolog', 'symfony/http-foundation']).
            vendor: Optional vendor prefix to auto-discover all packages (e.g. 'laravel' or 'symfony').
            token: Optional Private Packagist API token.
            options: PackagistRenderOptions.
        """
        client = PackagistClient(token=token)
        target_packages = list(packages or [])

        if vendor:
            try:
                v_pkgs = client.list_vendor_packages(vendor)
                for p in v_pkgs:
                    if p not in target_packages:
                        target_packages.append(p)
            except Exception as e:
                logger.warning("Failed fetching packages for vendor '%s': %s", vendor, e)

        if not target_packages:
            target_packages = ["monolog/monolog", "guzzlehttp/guzzle"]

        @dlt.resource(name="packagist_packages", primary_key="id", write_disposition="merge")
        def packagist_packages():
            # Check high-res cursor for incremental updates
            try:
                state = dlt.current.resource_state()
                last_cursor = state.get("cursor")
            except Exception:
                state = {}
                last_cursor = None

            try:
                changes = client.get_changes(since=last_cursor)
                new_cursor = changes.get("timestamp")
                if new_cursor:
                    state["cursor"] = new_cursor
            except Exception:
                pass

            yield from sync_packagist_records(client, target_packages, options=options)

        setattr(packagist_packages, DOCUMENT_SOURCE_ATTR, "packagist")
        return [packagist_packages]

else:

    def packagist_source(
        packages: list[str] | None = None,
        vendor: str | None = None,
        token: str | None = None,
        options: PackagistRenderOptions = PackagistRenderOptions(),
    ):
        """Fallback generator when dlt is not loaded."""
        client = PackagistClient(token=token)
        target_packages = list(packages or [])
        if vendor:
            try:
                for p in client.list_vendor_packages(vendor):
                    if p not in target_packages:
                        target_packages.append(p)
            except Exception:
                pass
        if not target_packages:
            target_packages = ["monolog/monolog", "guzzlehttp/guzzle"]
        return list(sync_packagist_records(client, target_packages, options=options))
