"""Cognee Community Hybrid Adapter - ArcadeDB

ArcadeDB multi-model database adapter providing both graph (via Bolt/Cypher)
and vector search (via HTTP API with HNSW indexes) for the Cognee framework.
"""

from .arcadedb_adapter import ArcadeDBAdapter

__version__ = "0.2.0"
__all__ = ["ArcadeDBAdapter"]
