"""Cognee Hex.pm BEAM package registry data-source connector."""

from cognee_community_connector_hexpm.client import (
    HexApiError,
    HexClient,
    HexPackageNotFoundError,
    HexRateLimitError,
)
from cognee_community_connector_hexpm.formatter import (
    hex_to_cognee_document,
    render_hex_markdown,
)
from cognee_community_connector_hexpm.hexpm import (
    DOCUMENT_SOURCE_ATTR,
    HexConnector,
    hexpm_source,
)
from cognee_community_connector_hexpm.models import (
    HexDependency,
    HexPackage,
    HexReleaseDetail,
    HexReleaseSummary,
    HexRetirement,
    RenderOptions,
)

__all__ = [
    "DOCUMENT_SOURCE_ATTR",
    "HexApiError",
    "HexClient",
    "HexConnector",
    "HexDependency",
    "HexPackage",
    "HexPackageNotFoundError",
    "HexRateLimitError",
    "HexReleaseDetail",
    "HexReleaseSummary",
    "HexRetirement",
    "RenderOptions",
    "hex_to_cognee_document",
    "hexpm_source",
    "render_hex_markdown",
]
