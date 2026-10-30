"""Cognee Community Graph Adapter - TypeDB

This package provides a TypeDB graph database adapter for the Cognee framework.
"""

from .register import register
from .typedb_adapter import TypeDBAdapter
from .TypeDBDatasetDatabaseHandler import TypeDBDatasetDatabaseHandler

__version__ = "0.1.0"
__all__ = ["TypeDBAdapter", "TypeDBDatasetDatabaseHandler", "register"]
