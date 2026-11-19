"""LodeDB vector adapter for cognee.

The adapter implementation lives in the ``lodedb`` package
(``lodedb.local.integrations.cognee.CogneeLodeDBAdapter``), where it is maintained and
tested against LodeDB's API and versioned with LodeDB releases. This module re-exports
it under the cognee-community naming so :mod:`register` can register the provider.

LodeDB is a local-first, on-disk, in-process vector store (no server, no account, no
API key), so the adapter treats ``vector_db_url`` as a base directory and keeps one
LodeDB index per cognee collection under it.
"""

try:
    from lodedb.local.integrations.cognee import CogneeLodeDBAdapter, IndexSchema
except ImportError as exc:  # pragma: no cover - clear install hint
    raise ImportError(
        "cognee-community-vector-adapter-lodedb needs a LodeDB build that ships the "
        "cognee adapter (lodedb.local.integrations.cognee). Install/upgrade with "
        "'pip install \"lodedb[cognee]\"'."
    ) from exc

# Alias to the cognee-community adapter naming convention (e.g. QDrantAdapter).
LodeDBAdapter = CogneeLodeDBAdapter

__all__ = ["CogneeLodeDBAdapter", "LodeDBAdapter", "IndexSchema"]
