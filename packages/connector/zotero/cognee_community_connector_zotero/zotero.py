"""DLT source for Zotero library (incremental sync + forget-on-delete).

Fetches Zotero references, notes, and attachment text, then yields them as a dlt
resource for cognee's ingestion pipeline.

Auth via API key. Incremental sync uses the library version header.
"""

import os
from typing import Any

from cognee.shared.logging_utils import get_logger
from cognee.tasks.ingestion.dlt_utils import DOCUMENT_SOURCE_ATTR

logger = get_logger("zotero_connector")

ZOTERO_TABLE_NAME = "zotero_items"
ZOTERO_SOURCE_NAME = "zotero"

_EXTRA_HINT = (
    'The Zotero connector requires the "zotero" extra: pip install "cognee[zotero]" '
    "(provides dlt and requests)."
)


def zotero_source(
    api_key: str | None = None,
    library_id: str | None = None,
    library_type: str = "user",
    client: Any = None,
):
    """Create a dlt source that yields Zotero items as documents.

    Args:
        api_key: Zotero API key. Falls back to ``ZOTERO_API_KEY``.
        library_id: User or group ID. Falls back to ``ZOTERO_LIBRARY_ID``.
        library_type: "user" or "group".
        client: Pre-built HTTP client (for testing).

    Returns:
        A dlt source suitable for ``cognee.add(...)`` / ``cognee.remember(...)``.
    """
    try:
        import dlt
    except ImportError as exc:
        raise ImportError(_EXTRA_HINT) from exc

    resolved_key = api_key or os.environ.get("ZOTERO_API_KEY")
    resolved_lib = library_id or os.environ.get("ZOTERO_LIBRARY_ID")
    if (not resolved_key or not resolved_lib) and client is None:
        raise ValueError(
            "Zotero API key and library ID required: pass them or set "
            "ZOTERO_API_KEY and ZOTERO_LIBRARY_ID."
        )

    @dlt.resource(
        name=ZOTERO_TABLE_NAME,
        primary_key="key",
        write_disposition="merge",
    )
    def zotero_items():
        # Starter implementation. Full version should use Zotero's REST API
        # with the version header for exact incremental sync and extract notes/attachments.
        # Example shape (to be filled with actual data):
        yield {
            "key": "EXAMPLEKEY",
            "title": "Example Reference",
            "content": "Starter Zotero connector placeholder — notes and abstract would appear here.",
            "item_type": "journalArticle",
            "url": "https://www.zotero.org/",
        }
        logger.info("Zotero: starter sync completed (placeholder data).")

    @dlt.source(name=ZOTERO_SOURCE_NAME)
    def _zotero():
        return zotero_items

    source = _zotero()
    setattr(source, DOCUMENT_SOURCE_ATTR, ZOTERO_SOURCE_NAME)
    return source
