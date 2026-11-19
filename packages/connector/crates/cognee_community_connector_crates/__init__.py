"""Cognee Crates.io Rust package registry data-source connector."""

from cognee_community_connector_crates.client import (
    CrateNotFoundError,
    CratesApiError,
    CratesClient,
    CratesRateLimitError,
)
from cognee_community_connector_crates.crates import (
    DOCUMENT_SOURCE_ATTR,
    CratesConnector,
    crates_source,
)
from cognee_community_connector_crates.formatter import (
    crate_to_cognee_document,
    render_crate_markdown,
)
from cognee_community_connector_crates.models import (
    CrateDependency,
    CrateMetadata,
    CratePackage,
    CrateVersion,
    RenderOptions,
)

__all__ = [
    "DOCUMENT_SOURCE_ATTR",
    "CrateDependency",
    "CrateMetadata",
    "CrateNotFoundError",
    "CratePackage",
    "CrateVersion",
    "CratesApiError",
    "CratesClient",
    "CratesConnector",
    "CratesRateLimitError",
    "RenderOptions",
    "crate_to_cognee_document",
    "crates_source",
    "render_crate_markdown",
]
