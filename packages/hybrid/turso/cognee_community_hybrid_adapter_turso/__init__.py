"""
Cognee community hybrid adapter for Turso/libSQL.

The adapter is intended to provide graph and vector storage through a
single libSQL/Turso backend.
"""

from .register import register

__all__ = ["register"]
