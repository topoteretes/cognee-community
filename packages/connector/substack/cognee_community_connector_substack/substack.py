"""DLT source for Substack newsletters (incremental sync + forget-on-delete).

Fetches Substack posts via RSS, then yields them as a dlt resource for cognee's
ingestion pipeline.

No auth required (public RSS). Incremental sync uses feed pubDate.
"""

import os
from typing import Any

from cognee.shared.logging_utils import get_logger
from cognee.tasks.ingestion.dlt_utils import DOCUMENT_SOURCE_ATTR

logger = get_logger("substack_connector")

SUBSTACK_TABLE_NAME = "substack_posts"
SUBSTACK_SOURCE_NAME = "substack"

_EXTRA_HINT = (
    'The Substack connector requires the "substack" extra: pip install "cognee[substack]" '
    "(provides dlt and feedparser)."
)


def substack_source(
    feed_url: str | None = None,
    client: Any = None,
):
    """Create a dlt source that yields Substack posts as documents.

    Args:
        feed_url: Substack RSS URL (e.g. https://example.substack.com/feed).
                  Falls back to ``SUBSTACK_FEED_URL``.
        client: Pre-built parser (for testing).

    Returns:
        A dlt source suitable for ``cognee.add(...)`` / ``cognee.remember(...)``.
    """
    try:
        import dlt
    except ImportError as exc:
        raise ImportError(_EXTRA_HINT) from exc

    resolved_url = feed_url or os.environ.get("SUBSTACK_FEED_URL")
    if not resolved_url and client is None:
        raise ValueError(
            "Substack feed URL required: pass feed_url= or set SUBSTACK_FEED_URL."
        )

    @dlt.resource(
        name=SUBSTACK_TABLE_NAME,
        primary_key="id",
        write_disposition="merge",
    )
    def substack_posts():
        # Starter implementation. Full version should parse the RSS feed
        # with feedparser and use pubDate for incremental sync.
        # Example shape (to be filled with actual data):
        yield {
            "id": "example-post-id",
            "title": "Example Newsletter Post",
            "content": "Starter Substack connector placeholder — full post content would appear here.",
            "published": "2026-01-01T00:00:00Z",
            "url": "https://example.substack.com/p/example",
        }
        logger.info("Substack: starter sync completed (placeholder data).")

    @dlt.source(name=SUBSTACK_SOURCE_NAME)
    def _substack():
        return substack_posts

    source = _substack()
    setattr(source, DOCUMENT_SOURCE_ATTR, SUBSTACK_SOURCE_NAME)
    return source
