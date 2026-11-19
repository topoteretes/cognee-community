"""Hypothesis (hypothes.is) connector package for Cognee Community."""

from .hypothesis import (
    HypothesisAnnotation,
    HypothesisClient,
    aggregate_annotations_by_url,
    extract_exact_quote,
    extract_page_title,
    hypothesis_pages_resource,
    hypothesis_source,
    parse_raw_annotation,
)

__all__ = [
    "HypothesisAnnotation",
    "HypothesisClient",
    "aggregate_annotations_by_url",
    "extract_exact_quote",
    "extract_page_title",
    "hypothesis_pages_resource",
    "hypothesis_source",
    "parse_raw_annotation",
]
