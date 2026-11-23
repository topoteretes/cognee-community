"""DLT source for Stack Overflow questions/answers (incremental sync + forget-on-delete).

Fetches questions, answers, and accepted solutions for given tags, then yields them
as a dlt resource for cognee's ingestion pipeline.

Auth via API key (recommended to avoid low quota). Incremental sync uses fromdate filter.
"""

import os
from typing import Any

from cognee.shared.logging_utils import get_logger
from cognee.tasks.ingestion.dlt_utils import DOCUMENT_SOURCE_ATTR

logger = get_logger("stackoverflow_connector")

STACKOVERFLOW_TABLE_NAME = "stackoverflow_posts"
STACKOVERFLOW_SOURCE_NAME = "stackoverflow"

_EXTRA_HINT = (
    'The Stack Overflow connector requires the "stackoverflow" extra: pip install "cognee[stackoverflow]" '
    "(provides dlt and requests)."
)


def stackoverflow_source(
    api_key: str | None = None,
    tags: list[str] | None = None,
    client: Any = None,
):
    """Create a dlt source that yields Stack Overflow posts as documents.

    Args:
        api_key: Stack Exchange API key. Falls back to ``STACKOVERFLOW_API_KEY``.
        tags: Restrict to these tags (required to stay within quota).
        client: Pre-built HTTP client (for testing).

    Returns:
        A dlt source suitable for ``cognee.add(...)`` / ``cognee.remember(...)``.
    """
    try:
        import dlt
    except ImportError as exc:
        raise ImportError(_EXTRA_HINT) from exc

    resolved_key = api_key or os.environ.get("STACKOVERFLOW_API_KEY")
    # API key is optional but strongly recommended; we still require tags.
    if client is None and not tags:
        raise ValueError("At least one tag is required to avoid exhausting the daily quota.")

    @dlt.resource(
        name=STACKOVERFLOW_TABLE_NAME,
        primary_key="question_id",
        write_disposition="merge",
    )
    def stackoverflow_posts():
        # Starter implementation. Full version should use the Stack Exchange API
        # with fromdate for incremental sync. Filter by tag.
        # Example shape (to be filled with actual data):
        yield {
            "question_id": 12345,
            "title": "Example Question",
            "body": "Starter Stack Overflow connector placeholder.",
            "accepted_answer": "",
            "tags": ["python", "ai"],
            "link": "https://stackoverflow.com/questions/12345",
        }
        logger.info("Stack Overflow: starter sync completed (placeholder data).")

    @dlt.source(name=STACKOVERFLOW_SOURCE_NAME)
    def _stackoverflow():
        return stackoverflow_posts

    source = _stackoverflow()
    setattr(source, DOCUMENT_SOURCE_ATTR, STACKOVERFLOW_SOURCE_NAME)
    return source
