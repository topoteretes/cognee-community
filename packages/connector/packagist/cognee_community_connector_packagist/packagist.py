"""Packagist / Composer Data-Source Connector for Cognee.

Tracks PHP Composer package metadata, abandonment notices, release versions,
and Packagist Security Advisories (PKSA / CVE).
"""

import json
import urllib.request
import urllib.parse
from typing import Dict, List, Any, Optional, Iterator
import hashlib
from datetime import datetime, timezone

# Optional dlt import for Cognee ecosystem compatibility
try:
    import dlt
    _has_dlt = True
except ImportError:
    dlt = None
    _has_dlt = False

def _optional_source(func):
    if _has_dlt and hasattr(dlt, "source"):
        return dlt.source(func)
    return func

def _optional_resource(*args, **kwargs):
    def decorator(func):
        if _has_dlt and hasattr(dlt, "resource"):
            return dlt.resource(*args, **kwargs)(func)
        return func
    return decorator


class PackagistClient:
    """Client for Packagist public REST APIs and Composer metadata feeds."""

    DEFAULT_BASE_URL = "https://packagist.org"
    DEFAULT_REPO_URL = "https://repo.packagist.org"

    def __init__(
        self,
        token: Optional[str] = None,
        base_url: Optional[str] = None,
        user_agent: Optional[str] = None
    ):
        self.token = token
        self.base_url = (base_url or self.DEFAULT_BASE_URL).rstrip("/")
        self.user_agent = user_agent or "PackagistCogneeConnector/1.0 (https://github.com/topoteretes/cognee-community)"

    def _request(
        self,
        endpoint: str,
        method: str = "GET",
        data: Optional[bytes] = None,
        content_type: Optional[str] = None
    ) -> Dict[str, Any]:
        url = endpoint if endpoint.startswith("http") else f"{self.base_url}/{endpoint.lstrip('/')}"
        headers = {
            "User-Agent": self.user_agent,
            "Accept": "application/json"
        }
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        if content_type:
            headers["Content-Type"] = content_type

        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        with urllib.request.urlopen(req, timeout=15) as resp:
            content = resp.read().decode("utf-8")
            return json.loads(content)

    def get_package_info(self, vendor: str, package: str) -> Dict[str, Any]:
        """Fetch package metadata from packagist.org/packages/{vendor}/{package}.json."""
        endpoint = f"packages/{vendor}/{package}.json"
        return self._request(endpoint)

    def get_vendor_packages(self, vendor: str) -> List[str]:
        """Fetch all packages for a vendor via /packages/list.json?vendor={vendor}."""
        endpoint = f"packages/list.json?vendor={urllib.parse.quote(vendor)}"
        res = self._request(endpoint)
        return res.get("packageNames", [])

    def get_security_advisories(self, package_names: List[str]) -> Dict[str, List[Dict[str, Any]]]:
        """Fetch official security advisories for packages via POST /api/security-advisories/."""
        if not package_names:
            return {}
        # Form-urlencoded data with packages[]=...
        query_params = [("packages[]", pkg) for pkg in package_names]
        encoded_data = urllib.parse.urlencode(query_params).encode("utf-8")
        res = self._request(
            endpoint="api/security-advisories/",
            method="POST",
            data=encoded_data,
            content_type="application/x-www-form-urlencoded"
        )
        return res.get("advisories", {})

    def get_metadata_changes(self, since_timestamp: Optional[int] = None) -> Dict[str, Any]:
        """Query /metadata/changes.json for incremental change detection."""
        endpoint = "metadata/changes.json"
        if since_timestamp is not None:
            endpoint += f"?since={since_timestamp}"
        return self._request(endpoint)


def format_package_document(
    package_data: Dict[str, Any],
    advisories: Optional[List[Dict[str, Any]]] = None
) -> Dict[str, Any]:
    """Formats package metadata and security advisories into a structured Cognee document."""
    pkg = package_data.get("package", {})
    name = pkg.get("name", "unknown")
    description = pkg.get("description", "")
    maintainers = [m.get("name") for m in pkg.get("maintainers", []) if m.get("name")]
    repository = pkg.get("repository", "")
    downloads = pkg.get("downloads", {})
    total_downloads = downloads.get("total", 0)
    monthly_downloads = downloads.get("monthly", 0)
    
    abandoned = pkg.get("abandoned", False)
    replacement_package = abandoned if isinstance(abandoned, str) else None

    # Latest version extraction
    versions_dict = pkg.get("versions", {})
    latest_version = None
    latest_release_time = None
    requires = {}
    suggests = {}

    for ver_name, ver_info in versions_dict.items():
        if "dev" in ver_name:
            continue
        latest_version = ver_name
        latest_release_time = ver_info.get("time")
        requires = ver_info.get("require", {})
        suggests = ver_info.get("suggest", {})
        break

    doc_id = hashlib.sha256(f"packagist:{name}".encode("utf-8")).hexdigest()

    markdown_text = f"# Composer Package: {name}\n\n"
    markdown_text += f"**Description:** {description}\n"
    markdown_text += f"**Repository:** {repository}\n"
    markdown_text += f"**Total Downloads:** {total_downloads:,} ({monthly_downloads:,} monthly)\n"
    markdown_text += f"**Maintainers:** {', '.join(maintainers)}\n"

    if abandoned:
        if replacement_package:
            markdown_text += f"\n⚠️ **PACKAGE ABANDONED:** Maintainer recommends using `{replacement_package}` instead.\n"
        else:
            markdown_text += "\n⚠️ **PACKAGE ABANDONED:** This package is no longer maintained.\n"

    if latest_version:
        markdown_text += f"\n### Latest Release: {latest_version} ({latest_release_time})\n"
        if requires:
            markdown_text += "**Requirements:**\n"
            for dep, constraint in requires.items():
                markdown_text += f"- `{dep}`: {constraint}\n"

    advisory_count = len(advisories or [])
    if advisory_count > 0:
        markdown_text += f"\n### 🛡️ Security Advisories ({advisory_count})\n"
        for adv in advisories or []:
            adv_id = adv.get("advisoryId", "")
            title = adv.get("title", "")
            cve = adv.get("cve", "N/A")
            link = adv.get("link", "")
            affected = adv.get("affectedVersions", "")
            markdown_text += f"- **{adv_id}** ({cve}): {title}\n"
            markdown_text += f"  - Affected Versions: `{affected}`\n"
            if link:
                markdown_text += f"  - Link: {link}\n"

    return {
        "id": doc_id,
        "name": name,
        "vendor": name.split("/")[0] if "/" in name else name,
        "package_name": name.split("/")[1] if "/" in name else name,
        "description": description,
        "repository": repository,
        "abandoned": bool(abandoned),
        "replacement_package": replacement_package,
        "total_downloads": total_downloads,
        "latest_version": latest_version,
        "advisory_count": advisory_count,
        "advisories": advisories or [],
        "text": markdown_text,
        "_deleted": False,
        "updated_at": datetime.now(timezone.utc).isoformat()
    }


def format_tombstone(package_name: str) -> Dict[str, Any]:
    """Generates a _deleted tombstone row for orphan cleanup in Cognee graph."""
    doc_id = hashlib.sha256(f"packagist:{package_name}".encode("utf-8")).hexdigest()
    return {
        "id": doc_id,
        "name": package_name,
        "_deleted": True,
        "text": "",
        "updated_at": datetime.now(timezone.utc).isoformat()
    }


@_optional_source
def packagist_source(
    packages: Optional[List[str]] = None,
    vendor: Optional[str] = None,
    client: Optional[PackagistClient] = None,
    exclude_abandoned: bool = False
):
    """Cognee dlt source for Composer/Packagist packages and security advisories."""
    client = client or PackagistClient()

    target_packages = list(packages or [])
    if vendor:
        vendor_pkgs = client.get_vendor_packages(vendor)
        target_packages.extend([p for p in vendor_pkgs if p not in target_packages])

    if not target_packages:
        raise ValueError("Must provide either a list of packages or a vendor prefix.")

    @_optional_resource(write_disposition="merge", primary_key="id")
    def packages_resource() -> Iterator[Dict[str, Any]]:
        # 1. Fetch security advisories in batch
        all_advisories = client.get_security_advisories(target_packages)

        # 2. Iterate each package
        for pkg_name in target_packages:
            if "/" not in pkg_name:
                continue
            v, p = pkg_name.split("/", 1)
            try:
                raw_info = client.get_package_info(v, p)
                advisories = all_advisories.get(pkg_name, [])
                doc = format_package_document(raw_info, advisories)
                if exclude_abandoned and doc.get("abandoned"):
                    yield format_tombstone(pkg_name)
                else:
                    yield doc
            except urllib.error.HTTPError as e:
                if e.code == 404:
                    # Package deleted or not found: yield tombstone
                    yield format_tombstone(pkg_name)
                else:
                    raise

    return packages_resource()
