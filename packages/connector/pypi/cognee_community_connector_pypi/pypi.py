"""PyPI exact-release metadata connector for cognee.

Fetches release-specific descriptions, declared dependencies, Python version
requirements, distribution metadata, and yanked status from the public PyPI JSON API
and yields documents for cognee memory ingestion.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from typing import Any

import httpx
from packaging.utils import canonicalize_name

try:
    from cognee.tasks.ingestion.dlt_utils import DOCUMENT_SOURCE_ATTR, PIPELINE_SCOPE_ATTR
except ImportError:
    DOCUMENT_SOURCE_ATTR = "cognee_document_source"
    PIPELINE_SCOPE_ATTR = "cognee_pipeline_scope"

DEFAULT_PYPI_BASE_URL = "https://pypi.org"
PYPI_SOURCE_NAME = "pypi"


class PyPIError(Exception):
    """Base exception for PyPI connector operations."""


class PyPIPackageNotFoundError(PyPIError):
    """Raised when a project or version is not found on PyPI (HTTP 404)."""


class PyPIValidationError(PyPIError):
    """Raised when the fetched payload does not match expected package identity."""


def normalize_project_name(name: str) -> str:
    """Normalize project name according to PyPA specification (PEP 503)."""
    return canonicalize_name(name)


def parse_release_spec(spec: str | tuple[str, str]) -> tuple[str, str]:
    """Parse a release specification into a (normalized_project, version) tuple.

    Supports:
        - ("requests", "2.31.0") -> ("requests", "2.31.0")
        - "requests==2.31.0"     -> ("requests", "2.31.0")
        - "requests@2.31.0"      -> ("requests", "2.31.0")
    """
    if isinstance(spec, (tuple, list)):
        if len(spec) != 2:
            raise ValueError(f"Expected a (project, version) pair, got: {spec}")
        return normalize_project_name(spec[0]), str(spec[1]).strip()

    if isinstance(spec, str):
        if "==" in spec:
            proj, ver = spec.split("==", 1)
            return normalize_project_name(proj.strip()), ver.strip()
        if "@" in spec:
            proj, ver = spec.split("@", 1)
            return normalize_project_name(proj.strip()), ver.strip()
        msg = (
            "String release spec must use 'project==version' or 'project@version' "
            f"format, got: {spec!r}"
        )
        raise ValueError(msg)

    raise TypeError(f"Invalid release spec type {type(spec).__name__}: {spec}")


def fetch_pypi_release(
    project: str,
    version: str,
    client: httpx.Client | None = None,
    base_url: str = DEFAULT_PYPI_BASE_URL,
    timeout: float = 15.0,
) -> dict[str, Any]:
    """Fetch release metadata for a specific (project, version) from PyPI JSON API.

    Docs: https://docs.pypi.org/api/json/
    Endpoint: GET /pypi/{project}/{version}/json
    """
    norm_project = normalize_project_name(project)
    clean_version = version.strip()
    url = f"{base_url.rstrip('/')}/pypi/{norm_project}/{clean_version}/json"

    should_close = False
    if client is None:
        client = httpx.Client(
            timeout=timeout,
            headers={"User-Agent": "cognee-community-connector-pypi/0.1.0"},
            follow_redirects=True,
        )
        should_close = True

    try:
        response = client.get(url)
        if response.status_code == 404:
            raise PyPIPackageNotFoundError(
                f"Release {norm_project}=={clean_version} not found on PyPI ({url})"
            )
        response.raise_for_status()
        data = response.json()
    except httpx.HTTPStatusError as exc:
        raise PyPIError(f"HTTP error fetching {url}: {exc.response.status_code}") from exc
    except httpx.RequestError as exc:
        raise PyPIError(f"Network error requesting {url}: {exc}") from exc
    finally:
        if should_close:
            client.close()

    # Identity validation
    info = data.get("info") or {}
    returned_name = info.get("name", "")
    returned_version = info.get("version", "")

    if normalize_project_name(returned_name) != norm_project:
        raise PyPIValidationError(
            f"PyPI returned project name '{returned_name}', expected '{norm_project}'"
        )

    # Check version match (accounting for potential PEP 440 normalization differences)
    if returned_version != clean_version and returned_version.lower() != clean_version.lower():
        raise PyPIValidationError(
            f"PyPI returned version '{returned_version}', expected '{clean_version}'"
        )

    return data


def render_release_document(
    project: str,
    version: str,
    payload: dict[str, Any],
) -> dict[str, Any]:
    """Render a PyPI release payload into a cognee document-mode record."""
    info = payload.get("info") or {}
    raw_files = payload.get("urls") or []

    norm_project = normalize_project_name(project)
    canonical_version = info.get("version") or version

    name = info.get("name") or norm_project
    summary = (info.get("summary") or "").strip()
    description = (info.get("description") or "").strip()
    requires_python = info.get("requires_python") or ""
    requires_dist = info.get("requires_dist") or []
    classifiers = info.get("classifiers") or []
    license_str = info.get("license") or ""
    author = info.get("author") or ""
    maintainer = info.get("maintainer") or ""
    home_page = info.get("home_page") or ""
    project_urls = info.get("project_urls") or {}

    yanked = bool(info.get("yanked"))
    yanked_reason = info.get("yanked_reason") or ""

    # Process distribution files
    distributions: list[dict[str, Any]] = []
    for f in raw_files:
        digests = f.get("digests") or {}
        distributions.append(
            {
                "filename": f.get("filename", ""),
                "packagetype": f.get("packagetype", ""),
                "size": f.get("size", 0),
                "upload_time": f.get("upload_time_iso_8601") or f.get("upload_time", ""),
                "sha256": digests.get("sha256", ""),
                "yanked": bool(f.get("yanked")),
                "yanked_reason": f.get("yanked_reason") or "",
            }
        )

    # Build rich markdown content for cognee cognify extraction
    lines: list[str] = [
        f"# PyPI Package: {name} (Version: {canonical_version})",
        "",
    ]

    if yanked:
        warning_msg = f"Reason: {yanked_reason}" if yanked_reason else "No reason provided."
        lines.extend(
            [
                "> ⚠️ **WARNING: YANKED RELEASE**",
                f"> This release was yanked from PyPI by the publisher. {warning_msg}",
                "",
            ]
        )

    if summary:
        lines.extend([f"**Summary**: {summary}", ""])

    lines.append("## Release Metadata")
    lines.append(f"- **Package Name**: {name} (canonical: `{norm_project}`)")
    lines.append(f"- **Version**: {canonical_version}")
    if requires_python:
        lines.append(f"- **Requires-Python**: `{requires_python}`")
    if license_str:
        lines.append(f"- **License**: {license_str}")
    if author:
        lines.append(f"- **Author**: {author}")
    if maintainer:
        lines.append(f"- **Maintainer**: {maintainer}")
    if home_page:
        lines.append(f"- **Homepage**: {home_page}")

    if project_urls:
        lines.append("- **Project URLs**:")
        for label, url in project_urls.items():
            lines.append(f"  - {label}: {url}")
    lines.append("")

    if requires_dist:
        lines.append("## Declared Dependencies (`requires_dist`)")
        for req in requires_dist:
            lines.append(f"- `{req}`")
        lines.append("")

    if classifiers:
        lines.append("## Classifiers")
        for cls in classifiers:
            lines.append(f"- {cls}")
        lines.append("")

    if distributions:
        lines.append("## Distribution Files")
        for dist in distributions:
            y_info = " (YANKED)" if dist["yanked"] else ""
            dist_desc = (
                f"- **{dist['filename']}** ({dist['packagetype']}, "
                f"{dist['size']} bytes, uploaded: {dist['upload_time']}){y_info}"
            )
            lines.append(dist_desc)
            if dist["sha256"]:
                lines.append(f"  - SHA256: `{dist['sha256']}`")
        lines.append("")

    if description:
        lines.append("## Description / README")
        lines.append(description)
        lines.append("")

    content = "\n".join(lines)
    record_id = f"pypi:{norm_project}:{canonical_version}"

    return {
        "id": record_id,
        "title": f"PyPI Package: {name}=={canonical_version}",
        "content": content,
        "project": norm_project,
        "version": canonical_version,
        "summary": summary,
        "requires_python": requires_python,
        "dependencies": requires_dist,
        "yanked": yanked,
        "yanked_reason": yanked_reason if yanked else None,
        "license": license_str,
        "classifiers": classifiers,
        "files": distributions,
    }


def pypi_source(
    releases: Sequence[str | tuple[str, str]],
    *,
    client: httpx.Client | None = None,
    base_url: str = DEFAULT_PYPI_BASE_URL,
    timeout: float = 15.0,
):
    """Return a dlt resource yielding release metadata documents for cognee.

    Args:
        releases: Sequence of release specifications, e.g.:
            [("cognee", "1.6.3"), ("requests", "2.31.0")]
            or ["cognee==1.6.3", "requests==2.31.0"]
        client: Optional custom httpx.Client (e.g. for testing with mocks).
        base_url: Base PyPI URL (defaults to "https://pypi.org").
        timeout: HTTP request timeout in seconds.

    Returns:
        dlt resource configured with document-mode tags for cognee.
    """
    try:
        import dlt
    except ImportError as exc:
        raise ImportError(
            "The 'dlt' package is required to use pypi_source. "
            "Install it with: pip install 'dlt[sqlalchemy]>=1.9.0,<2'"
        ) from exc

    parsed_specs = [parse_release_spec(spec) for spec in releases]

    @dlt.resource(
        name="pypi_releases",
        primary_key="id",
        write_disposition="replace",
    )
    def pypi_releases_resource() -> Iterable[dict[str, Any]]:
        # Fetch and validate all configured releases before yielding (atomic replacement)
        records: list[dict[str, Any]] = []
        for proj, ver in parsed_specs:
            payload = fetch_pypi_release(
                proj,
                ver,
                client=client,
                base_url=base_url,
                timeout=timeout,
            )
            doc = render_release_document(proj, ver, payload)
            records.append(doc)

        yield from records

    resource = pypi_releases_resource()
    setattr(resource, DOCUMENT_SOURCE_ATTR, PYPI_SOURCE_NAME)
    setattr(resource, PIPELINE_SCOPE_ATTR, "pypi_releases")

    return resource
