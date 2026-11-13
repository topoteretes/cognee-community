"""Data models for Crates.io package registry items."""

from dataclasses import dataclass, field


@dataclass
class CrateDependency:
    """Dependency required by a Rust crate."""

    crate_id: str
    req: str
    kind: str = "normal"  # normal, dev, build
    optional: bool = False
    features: list[str] = field(default_factory=list)
    target: str | None = None


@dataclass
class CrateVersion:
    """Metadata for a specific release version of a crate."""

    num: str
    license: str | None = None
    downloads: int = 0
    features: dict[str, list[str]] = field(default_factory=dict)
    yanked: bool = False
    created_at: str | None = None
    updated_at: str | None = None


@dataclass
class CrateMetadata:
    """Top-level crate metadata from the crates.io API."""

    name: str
    description: str
    max_version: str
    documentation: str | None = None
    repository: str | None = None
    homepage: str | None = None
    downloads: int = 0
    recent_downloads: int | None = None
    keywords: list[str] = field(default_factory=list)
    categories: list[str] = field(default_factory=list)
    updated_at: str | None = None


@dataclass
class CratePackage:
    """Complete crate package representation ready for ingestion."""

    metadata: CrateMetadata
    latest_version: CrateVersion | None = None
    dependencies: list[CrateDependency] = field(default_factory=list)
    readme: str | None = None


@dataclass
class RenderOptions:
    """Configuration options for formatting crate markdown documents."""

    include_readme: bool = True
    include_dependencies: bool = True
    include_features: bool = True
    max_readme_chars: int = 15000
