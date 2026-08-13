"""Cognee Community Graph Adapter - ArcadeDB

This package provides an ArcadeDB graph database adapter for the Cognee framework.
ArcadeDB connects via the Neo4j Bolt wire protocol, so the standard neo4j Python
driver is used under the hood.
"""

from .arcadedb_adapter import ArcadeDBAdapter

__version__ = "0.2.0"
__all__ = ["ArcadeDBAdapter", "register"]


def register():
    """Register the ArcadeDB adapter with cognee's supported databases."""
    try:
        from cognee.infrastructure.databases.graph.supported_databases import (
            supported_databases,
        )

        supported_databases["arcadedb"] = ArcadeDBAdapter
    except ImportError as ie:
        raise ImportError(
            "cognee is not installed. Please install it with: pip install cognee"
        ) from ie
