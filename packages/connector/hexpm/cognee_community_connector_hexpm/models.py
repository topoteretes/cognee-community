"""Data models for Hex.pm package registry items."""

from dataclasses import dataclass, field
from typing import Any


@dataclass
class HexDependency:
    """Dependency requirement for a Hex release."""

    name: str
    app: str
    requirement: str
    optional: bool = False


@dataclass
class HexRetirement:
    """Retirement notice for an invalid, deprecated, or insecure release."""

    version: str
    reason: str  # invalid, security, deprecated, renamed, other
    message: str | None = None


@dataclass
class HexReleaseSummary:
    """Brief metadata for a package release listed in the package manifest."""

    version: str
    url: str
    has_docs: bool = False
    inserted_at: str | None = None


@dataclass
class HexReleaseDetail:
    """Detailed release metadata including dependencies, docs, and retirements."""

    version: str
    has_docs: bool = False
    docs_html_url: str | None = None
    checksum: str | None = None
    requirements: list[HexDependency] = field(default_factory=list)
    retirement: HexRetirement | None = None
    security_advisories: list[dict[str, Any]] = field(default_factory=list)
    downloads: int = 0
    inserted_at: str | None = None
    updated_at: str | None = None


@dataclass
class HexPackage:
    """Full Hex package entity ready for Cognee NLP entity extraction."""

    name: str
    description: str
    licenses: list[str] = field(default_factory=list)
    links: dict[str, str] = field(default_factory=dict)
    maintainers: list[str] = field(default_factory=list)
    latest_version: str = "0.1.0"
    latest_release: HexReleaseDetail | None = None
    all_retirements: dict[str, HexRetirement] = field(default_factory=dict)
    downloads: dict[str, int] = field(default_factory=dict)
    html_url: str = ""
    docs_html_url: str = ""
    inserted_at: str | None = None
    updated_at: str | None = None


@dataclass
class RenderOptions:
    """Options for formatting Hex package Markdown documents."""

    include_dependencies: bool = True
    include_retirements: bool = True
    include_security_advisories: bool = True
    exclude_retired: bool = False
