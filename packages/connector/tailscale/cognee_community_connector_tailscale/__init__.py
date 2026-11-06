"""Tailscale connector package for cognee."""

from .tailscale import (
    DEFAULT_BASE_URL,
    SUPPORTED_RESOURCES,
    TailscaleAPIError,
    TailscaleAuthError,
    TailscaleClient,
    TailscaleError,
    TailscaleQuotaError,
    TailscaleRateLimitedError,
    tailscale_source,
)

__all__ = [
    "DEFAULT_BASE_URL",
    "SUPPORTED_RESOURCES",
    "TailscaleAPIError",
    "TailscaleAuthError",
    "TailscaleClient",
    "TailscaleError",
    "TailscaleQuotaError",
    "TailscaleRateLimitedError",
    "tailscale_source",
]
