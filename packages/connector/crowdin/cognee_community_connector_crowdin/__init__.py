"""Crowdin localization, glossary & translation data-source connector for Cognee."""

from .crowdin import (
    CROWDIN_SOURCE_NAME,
    CROWDIN_TABLE_NAME,
    CrowdinAPIError,
    CrowdinAuthError,
    CrowdinClient,
    CrowdinConfig,
    CrowdinRateLimitError,
    crowdin_connector,
    crowdin_source,
)

__all__ = [
    "CROWDIN_SOURCE_NAME",
    "CROWDIN_TABLE_NAME",
    "CrowdinAPIError",
    "CrowdinAuthError",
    "CrowdinClient",
    "CrowdinConfig",
    "CrowdinRateLimitError",
    "crowdin_connector",
    "crowdin_source",
]
