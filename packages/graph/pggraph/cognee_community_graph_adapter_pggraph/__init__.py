"""Cognee Community Graph Adapter - pgGraph (experimental)."""

from .pggraph_adapter import PgGraphAdapter

__version__ = "0.1.0"
__all__ = ["PgGraphAdapter", "register"]


def register():
    """Register the pgGraph adapter with Cognee."""
    try:
        from cognee.infrastructure.databases.graph import use_graph_adapter

        use_graph_adapter("pggraph", PgGraphAdapter)
    except ImportError as ie:
        raise ImportError(
            "cognee is not installed. Install with: pip install cognee asyncpg"
        ) from ie
