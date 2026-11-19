"""Cognee community connector for Amazon Kindle highlights and notes (My Clippings.txt)."""

from .kindle import (
    KINDLE_SOURCE_NAME,
    KINDLE_TABLE_NAME,
    format_book_clippings_markdown,
    kindle_source,
    parse_book_title_and_author,
    parse_clipping_entry,
    parse_clippings_file,
)

__all__ = [
    "KINDLE_SOURCE_NAME",
    "KINDLE_TABLE_NAME",
    "format_book_clippings_markdown",
    "kindle_source",
    "parse_book_title_and_author",
    "parse_clipping_entry",
    "parse_clippings_file",
]
