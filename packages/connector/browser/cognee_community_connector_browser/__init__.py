"""Browser connector for cognee."""

from .browser import (
    BROWSER_SOURCE_NAME,
    BROWSER_TABLE_NAME,
    browser_source,
    detect_browser_profile,
)

__all__ = [
    "BROWSER_SOURCE_NAME",
    "BROWSER_TABLE_NAME",
    "browser_source",
    "detect_browser_profile",
]
