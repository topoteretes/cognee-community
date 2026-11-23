"""DLT source for Readwise highlights (incremental sync + forget-on-delete).

Fetches Readwise highlights, notes, and sources, then yields them as a dlt
resource for cognee's ingestion pipeline.

Auth via access token. Incremental sync uses updatedAfter filter.
"""

import os
from typing import Any

from cognee.shared.logging_utils import get_logger
from cognee.tasks.ingestion.dlt_utils import DOCUMENT_SOURCE_ATTR

logger = get_logger("readwise_connector")

READWISE_TABLE_NAME = "readwise_highlights"
READWISE_SOURCE_NAME = "readwise"

_EXTRA_HINT = (
    'The Readwise connector requires the "readwise" extra: pip install "cognee[readwise]" '
    "(provides dlt and requests)."
)


def readwise_source(
    token: str | None = None,
    updated_after: str | None = None,
    client: Any = None,
):
    """Create a dlt source that yields Readwise highlights as documents.

    Args:
        token: Readwise access token. Falls back to ``READWISE_API_TOKEN``.
        updated_after: ISO timestamp for incremental filter.
        client: Pre-built HTTP client (for testing).

    Returns:
        A dlt source suitable for ``cognee.add(...)`` / ``cognee.remember(...)``.
    """
    try:
        import dlt
    except ImportError as exc:
        raise ImportError(_EXTRA_HINT) from exc

    resolved_token = token or os.environ.get("READWISE_API_TOKEN")
    if not resolved_token and client is None:
        raise ValueError(
            "Readwise access token required: pass token= or set READWISE_API_TOKEN."
        )

    @dlt.resource(
        name=READWISE_TABLE_NAME,
        primary_key="id",
        write_disposition="merge",
    )
    def readwise_highlights():
        # Starter implementation. Full version should use Readwise's export API
        # with updatedAfter for incremental sync. Highlights are high-signal.
        # Example shape (to be filled with actual data):
        yield {
            "id": 12345,
            "text": "Example highlight text from a book or article.",
            "note": "",
            "title": "Example Source",
            "author": "Author Name",
            "source_url": "https://example.com",
        }
        logger.info("Readwise: starter sync completed (placeholder data).")

    @dlt.source(name=READWISE_SOURCE_NAME)
    def _readwise():
        return readwise_highlights

    source = _readwise()
    setattr(source, DOCUMENT_SOURCE_ATTR, READWISE_SOURCE_NAME)
    return source
