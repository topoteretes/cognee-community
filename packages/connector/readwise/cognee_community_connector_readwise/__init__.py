"""Readwise data-source connector for cognee (highlights + notes + sources)."""

from .readwise import (
    READWISE_SOURCE_NAME,
    READWISE_TABLE_BOOKS,
    READWISE_TABLE_HIGHLIGHTS,
    ReadwiseClient,
    readwise_source,
    sync_books,
    sync_highlights,
)

__all__ = [
    "READWISE_SOURCE_NAME",
    "READWISE_TABLE_BOOKS",
    "READWISE_TABLE_HIGHLIGHTS",
    "ReadwiseClient",
    "readwise_source",
    "sync_books",
    "sync_highlights",
]
