"""Crossref DOI metadata connector for cognee."""

from .crossref import (
    DOCUMENT_SOURCE_ATTR,
    crossref_source,
    format_crossref_document,
    normalize_doi,
    parse_crossref_works,
)

__all__ = [
    "DOCUMENT_SOURCE_ATTR",
    "normalize_doi",
    "format_crossref_document",
    "parse_crossref_works",
    "crossref_source",
]
