"""Vanta connector package for cognee."""

from .vanta import (
    DEFAULT_BASE_URL,
    SUPPORTED_RESOURCES,
    VantaAPIError,
    VantaAuthError,
    VantaClient,
    VantaError,
    VantaQuotaError,
    VantaRateLimitedError,
    vanta_source,
)

__all__ = [
    "DEFAULT_BASE_URL",
    "SUPPORTED_RESOURCES",
    "VantaAPIError",
    "VantaAuthError",
    "VantaClient",
    "VantaError",
    "VantaQuotaError",
    "VantaRateLimitedError",
    "vanta_source",
]
