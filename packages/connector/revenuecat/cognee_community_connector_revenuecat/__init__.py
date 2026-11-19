"""
RevenueCat Connector for Cognee.
Synchronizes in-app subscription entitlements, transaction events, and customer lifecycles into Cognee knowledge graphs.
"""

from .revenuecat import (
    RevenueCatConnector,
    fetch_subscriber_info,
    format_subscriber_markdown
)

__all__ = [
    "RevenueCatConnector",
    "fetch_subscriber_info",
    "format_subscriber_markdown"
]
