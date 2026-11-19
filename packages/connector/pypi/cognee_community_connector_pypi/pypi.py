"""PyPI Data-Source Connector for Cognee.

Fetches package metadata, release information, and README documentation
from the public Python Package Index (PyPI) JSON REST API:
https://pypi.org/pypi/{package_name}/json
"""

import json
import logging
import urllib.error
import urllib.request
from typing import Any, Dict, Iterator, List, Optional
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

PYPI_BASE_URL: str = "https://pypi.org/pypi"
DEFAULT_USER_AGENT: str = "cognee-community-connector-pypi/0.1.0"


class PyPIPackageDocument(BaseModel):
    """Structured representation of a Python package fetched from PyPI."""

    name: str = Field(description="Normalized package name")
    version: str = Field(description="Latest package release version")
    summary: str = Field(default="", description="One-line summary description")
    author: str = Field(default="", description="Package author or maintainer")
    author_email: Optional[str] = Field(default=None, description="Author contact email")
    license: str = Field(default="", description="Package software license")
    home_page: str = Field(default="", description="Home page or project URL")
    project_urls: Dict[str, str] = Field(
        default_factory=dict, description="Project links dictionary"
    )
    requires_python: Optional[str] = Field(
        default=None, description="Supported Python versions"
    )
    requires_dist: List[str] = Field(
        default_factory=list, description="Direct package dependencies"
    )
    keywords: str = Field(default="", description="Package keywords / topics")
    description: str = Field(
        default="", description="Complete package README documentation"
    )

    def to_markdown(self) -> str:
        """Converts package metadata and documentation into clean, structured Markdown.

        This format is optimized for Cognee's text chunking, embedding,
        and knowledge graph entity-relationship extraction.
        """
        lines = [
            f"# {self.name} (v{self.version})",
            "",
            f"**Summary**: {self.summary or 'No summary provided.'}",
            f"**Author**: {self.author or 'Unknown'}",
            f"**License**: {self.license or 'Not specified'}",
            f"**Requires Python**: {self.requires_python or 'Any'}",
            "",
        ]

        if self.project_urls:
            lines.append("### Project Links")
            for label, url in sorted(self.project_urls.items()):
                lines.append(f"- **{label}**: {url}")
            lines.append("")

        if self.requires_dist:
            lines.append("### Key Dependencies")
            for dep in self.requires_dist[:15]:
                lines.append(f"- `{dep}`")
            if len(self.requires_dist) > 15:
                lines.append(f"- *...and {len(self.requires_dist) - 15} more dependencies*")
            lines.append("")

        lines.extend([
            "### Documentation & README",
            "",
            self.description or "*No README documentation available on PyPI.*",
        ])

        return "\n".join(lines)

    def to_cognee_document(self) -> Dict[str, Any]:
        """Converts the package document into standard Cognee document format."""
        return {
            "id": f"pypi_{self.name}_{self.version}".replace("-", "_").lower(),
            "title": f"PyPI: {self.name} {self.version}",
            "text": self.to_markdown(),
            "metadata": {
                "source": "pypi",
                "package_name": self.name,
                "version": self.version,
                "author": self.author,
                "license": self.license,
                "keywords": self.keywords,
            },
        }


def fetch_package_metadata(
    package_name: str,
    timeout: float = 15.0,
    user_agent: str = DEFAULT_USER_AGENT,
) -> PyPIPackageDocument:
    """Fetches package details from the PyPI JSON API.

    Args:
        package_name: Name of the Python package (e.g. 'fastapi', 'pydantic').
        timeout: Request timeout in seconds.
        user_agent: HTTP User-Agent header string.

    Returns:
        PyPIPackageDocument containing parsed package details.

    Raises:
        ValueError: If the package does not exist on PyPI (HTTP 404).
        RuntimeError: If network or PyPI API communication fails.
    """
    clean_name = package_name.strip()
    if not clean_name:
        raise ValueError("Package name cannot be empty.")

    url = f"{PYPI_BASE_URL}/{clean_name}/json"
    req = urllib.request.Request(
        url,
        headers={"User-Agent": user_agent, "Accept": "application/json"},
    )

    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as err:
        if err.code == 404:
            raise ValueError(f"Package '{clean_name}' was not found on PyPI (HTTP 404).") from err
        raise RuntimeError(f"PyPI API returned HTTP {err.code}: {err.reason}") from err
    except urllib.error.URLError as err:
        raise RuntimeError(f"Failed to connect to PyPI API: {err.reason}") from err
    except Exception as exc:
        raise RuntimeError(f"Unexpected error while fetching PyPI metadata: {exc}") from exc

    info = payload.get("info", {})

    return PyPIPackageDocument(
        name=info.get("name", clean_name),
        version=info.get("version", "unknown"),
        summary=info.get("summary") or "",
        author=info.get("author") or "",
        author_email=info.get("author_email"),
        license=info.get("license") or "",
        home_page=info.get("home_page") or "",
        project_urls=info.get("project_urls") or {},
        requires_python=info.get("requires_python"),
        requires_dist=info.get("requires_dist") or [],
        keywords=info.get("keywords") or "",
        description=info.get("description") or "",
    )


class PyPIConnector:
    """High-level connector for batch ingesting Python packages into Cognee."""

    def __init__(self, timeout: float = 15.0, user_agent: str = DEFAULT_USER_AGENT):
        self.timeout = timeout
        self.user_agent = user_agent

    def get_package(self, package_name: str) -> PyPIPackageDocument:
        """Fetch a single package document from PyPI."""
        return fetch_package_metadata(
            package_name=package_name,
            timeout=self.timeout,
            user_agent=self.user_agent,
        )

    def get_packages(
        self, package_names: List[str], ignore_missing: bool = True
    ) -> List[PyPIPackageDocument]:
        """Fetch multiple packages from PyPI.

        Args:
            package_names: List of package names to retrieve.
            ignore_missing: If True, missing packages log a warning rather than raising.
        """
        results: List[PyPIPackageDocument] = []
        for name in package_names:
            try:
                pkg = self.get_package(name)
                results.append(pkg)
            except ValueError as err:
                if ignore_missing:
                    logger.warning("Skipping missing package '%s': %s", name, err)
                else:
                    raise
            except Exception as exc:
                logger.error("Error retrieving package '%s': %s", name, exc)
                if not ignore_missing:
                    raise
        return results

    def fetch_documents(
        self, package_names: List[str], ignore_missing: bool = True
    ) -> List[Dict[str, Any]]:
        """Retrieves and formats package documents ready for cognee.add()."""
        packages = self.get_packages(package_names, ignore_missing=ignore_missing)
        return [pkg.to_cognee_document() for pkg in packages]


def pypi_source(package_names: List[str]) -> Iterator[Dict[str, Any]]:
    """Generator function yielding package data records for pipeline workflows."""
    connector = PyPIConnector()
    docs = connector.fetch_documents(package_names)
    for doc in docs:
        yield doc
