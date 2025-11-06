"""Ladybug graph adapter built on top of Cognee's Kuzu implementation."""

from __future__ import annotations

import importlib
import sys
from typing import Any

from cognee.infrastructure.databases.graph.config import get_graph_config
from cognee.shared.logging_utils import get_logger

logger = get_logger("LadybugAdapter")


def _alias_ladybug_as_kuzu() -> None:
    """Ensure Cognee's Kuzu adapter can reuse the Ladybug python bindings."""
    try:  # pragma: no cover - direct reuse when kuzu is already installed
        import kuzu  # noqa: F401  # type: ignore
        logger.debug("Detected native 'kuzu' package – using it directly.")
        return
    except ModuleNotFoundError:
        pass

    try:
        ladybug_module = importlib.import_module("real_ladybug")
    except ModuleNotFoundError as exc:  # pragma: no cover - import-time validation
        raise ModuleNotFoundError(
            "Ladybug adapter requires the 'real_ladybug' package. "
            "Install it with `pip install real_ladybug`."
        ) from exc

    sys.modules.setdefault("kuzu", ladybug_module)

    # Mirror Ladybug submodules so that `from kuzu.database import Database` keeps working.
    for submodule in ("database", "connection", "async_connection", "prepared_statement"):
        try:
            sys.modules.setdefault(
                f"kuzu.{submodule}", importlib.import_module(f"real_ladybug.{submodule}")
            )
        except ModuleNotFoundError:  # pragma: no cover - optional modules
            continue

    logger.info("Patched Cognee to use Ladybug bindings in place of kuzu")


_alias_ladybug_as_kuzu()

from cognee.infrastructure.databases.graph.kuzu.adapter import (  # noqa: E402
    KuzuAdapter as BaseKuzuAdapter,
)


class LadybugAdapter(BaseKuzuAdapter):
    """Graph adapter that reuses Cognee's Kuzu logic with Ladybug bindings."""

    name = "Ladybug"

    def __init__(self, graph_database_url: str | None = None, **_: Any) -> None:
        db_path = graph_database_url or get_graph_config().graph_file_path
        if not db_path:
            raise ValueError(
                "Ladybug adapter needs a database path. Set `graph_database_url` "
                "via `cognee.config.set_graph_db_config`."
            )

        self._ladybug_db_path = db_path
        super().__init__(db_path=db_path)

    @property
    def database_path(self) -> str:
        """Expose the resolved Ladybug database path (useful for tests/debug)."""
        return self._ladybug_db_path
