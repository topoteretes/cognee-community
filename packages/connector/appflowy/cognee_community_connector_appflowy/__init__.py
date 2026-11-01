"""AppFlowy knowledge base, workspace & database data-source connector for Cognee."""

from .appflowy import (
    APPFLOWY_SOURCE_NAME,
    APPFLOWY_TABLE_NAME,
    AppFlowyAPIError,
    AppFlowyAuthError,
    AppFlowyClient,
    AppFlowyConfig,
    AppFlowyRateLimitError,
    appflowy_connector,
    appflowy_source,
)

__all__ = [
    "APPFLOWY_SOURCE_NAME",
    "APPFLOWY_TABLE_NAME",
    "AppFlowyAPIError",
    "AppFlowyAuthError",
    "AppFlowyClient",
    "AppFlowyConfig",
    "AppFlowyRateLimitError",
    "appflowy_connector",
    "appflowy_source",
]
