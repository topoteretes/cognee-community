"""Cognee Community Graph Adapter - Ladybug.

This adapter lets Cognee use the Ladybug (formerly Kùzu) embedded graph database.
"""

from .ladybug_adapter import LadybugAdapter

__all__ = ["LadybugAdapter", "register"]


def register() -> None:
    """Register the Ladybug adapter with Cognee's supported graph databases."""
    try:
        from cognee.infrastructure.databases.graph.supported_databases import (
            supported_databases,
        )
    except ImportError as exc:  # pragma: no cover - validation for missing cognee
        raise ImportError(
            "cognee is not installed. Install it via `pip install cognee`."
        ) from exc

    supported_databases["ladybug"] = LadybugAdapter
