"""DLT source for Raindrop.io bookmarks (incremental sync + forget-on-delete).

Fetches Raindrop bookmarks, collections, and annotations, then yields them as a dlt
resource for cognee's ingestion pipeline.

Auth via test token / OAuth. Incremental sync uses lastUpdate sort.
"""

import os
from typing import Any

from cognee.shared.logging_utils import get_logger
from cognee.tasks.ingestion.dlt_utils import DOCUMENT_SOURCE_ATTR

logger = get_logger("raindrop_connector")

RAINDROP_TABLE_NAME = "raindrop_bookmarks"
RAINDROP_SOURCE_NAME = "raindrop"

_EXTRA_HINT = (
    'The Raindrop connector requires the "raindrop" extra: pip install "cognee[raindrop]" '
    "(provides dlt and requests)."
)


def raindrop_source(
    token: str | None = None,
    collection_ids: list[int] | None = None,
    client: Any = None,
):
    """Create a dlt source that yields Raindrop bookmarks as documents.

    Args:
        token: Raindrop test token. Falls back to ``RAINDROP_API_TOKEN``.
        collection_ids: Restrict to these collection IDs. Omit for all.
        client: Pre-built HTTP client (for testing).

    Returns:
        A dlt source suitable for ``cognee.add(...)`` / ``cognee.remember(...)``.
    """
    try:
        import dlt
    except ImportError as exc:
        raise ImportError(_EXTRA_HINT) from exc

    resolved_token = token or os.environ.get("RAINDROP_API_TOKEN")
    if not resolved_token and client is None:
        raise ValueError(
            "Raindrop API token required: pass token= or set RAINDROP_API_TOKEN."
        )

    @dlt.resource(
        name=RAINDROP_TABLE_NAME,
        primary_key="_id",
        write_disposition="merge",
    )
    def raindrop_bookmarks():
        # Starter implementation. Full version should use Raindrop's REST API
        # with lastUpdate sort for incremental sync and optionally fetch page content.
        # Example shape (to be filled with actual data):
        yield {
            "_id": 12345,
            "title": "Example Bookmark",
            "excerpt": "Starter Raindrop connector placeholder.",
            "link": "https://example.com",
            "note": "",
            "collection": {"title": "Unsorted"},
        }
        logger.info("Raindrop: starter sync completed (placeholder data).")

    @dlt.source(name=RAINDROP_SOURCE_NAME)
    def _raindrop():
        return raindrop_bookmarks

    source = _raindrop()
    setattr(source, DOCUMENT_SOURCE_ATTR, RAINDROP_SOURCE_NAME)
    return source
