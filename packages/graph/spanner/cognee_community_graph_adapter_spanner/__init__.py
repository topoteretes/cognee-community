"""Cognee Community Graph Adapter - Google Cloud Spanner (Spanner Graph).

This package provides a Google Cloud Spanner graph database adapter for Cognee
using Spanner Graph (GQL) for knowledge graphs and GraphRAG pipelines.
"""

from .spanner_adapter import SpannerGraphAdapter

__version__ = "0.2.0"
__all__ = ["SpannerGraphAdapter", "register"]


def register() -> None:
    """Register the Spanner Graph adapter with Cognee's supported databases."""
    try:
        from cognee.infrastructure.databases.graph import use_graph_adapter

        use_graph_adapter("spanner", SpannerGraphAdapter)
    except ImportError as ie:
        raise ImportError(
            "cognee is not installed. Please install it with: pip install cognee"
        ) from ie
