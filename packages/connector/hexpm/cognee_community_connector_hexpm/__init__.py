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
    "HexClient",
    "HexApiError",
    "HexPackageNotFoundError",
    "HexRateLimitError",
    "HexConnector",
    "hexpm_source",
    "DOCUMENT_SOURCE_ATTR",
    "HexDependency",
    "HexReleaseDetail",
    "HexReleaseSummary",
    "HexRetirement",
    "HexPackage",
    "RenderOptions",
    "render_hex_markdown",
    "hex_to_cognee_document",
]
