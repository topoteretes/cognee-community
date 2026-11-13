"""Rippling connector package for cognee."""

from .rippling import (
    DEFAULT_BASE_URL,
    SUPPORTED_RESOURCES,
    RipplingAPIError,
    RipplingAuthError,
    RipplingClient,
    RipplingError,
    RipplingQuotaError,
    RipplingRateLimitedError,
    rippling_source,
)

__all__ = [
    "DEFAULT_BASE_URL",
    "SUPPORTED_RESOURCES",
    "RipplingAPIError",
    "RipplingAuthError",
    "RipplingClient",
    "RipplingError",
    "RipplingQuotaError",
    "RipplingRateLimitedError",
    "rippling_source",
]
