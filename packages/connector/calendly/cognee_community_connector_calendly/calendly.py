"""DLT source for Calendly scheduled events (incremental sync + forget-on-delete).

Fetches Calendly scheduled events, invitee questions/answers, and notes,
then yields them as a dlt resource for cognee's ingestion pipeline.

Auth via personal access token. Incremental sync uses min_start_time window.
"""

import os
from datetime import datetime, timedelta, timezone
from typing import Any

from cognee.shared.logging_utils import get_logger
from cognee.tasks.ingestion.dlt_utils import DOCUMENT_SOURCE_ATTR

logger = get_logger("calendly_connector")

CALENDLY_TABLE_NAME = "calendly_events"
CALENDLY_SOURCE_NAME = "calendly"

_EXTRA_HINT = (
    'The Calendly connector requires the "calendly" extra: pip install "cognee[calendly]" '
    "(provides dlt and requests)."
)


def calendly_source(
    token: str | None = None,
    min_start_time: str | None = None,
    client: Any = None,
):
    """Create a dlt source that yields Calendly events as documents.

    Args:
        token: Calendly personal access token. Falls back to ``CALENDLY_API_TOKEN``.
        min_start_time: ISO timestamp for incremental window (default: 30 days ago).
        client: Pre-built HTTP client (for testing).

    Returns:
        A dlt source suitable for ``cognee.add(...)`` / ``cognee.remember(...)``.
    """
    try:
        import dlt
    except ImportError as exc:
        raise ImportError(_EXTRA_HINT) from exc

    resolved_token = token or os.environ.get("CALENDLY_API_TOKEN")
    if not resolved_token and client is None:
        raise ValueError(
            "Calendly personal access token required: pass token= or set CALENDLY_API_TOKEN."
        )

    @dlt.resource(
        name=CALENDLY_TABLE_NAME,
        primary_key="uri",
        write_disposition="merge",
    )
    def calendly_events():
        # Starter implementation. Full version should use Calendly's scheduled_events
        # endpoint with min_start_time for incremental sync and handle invitee questions.
        # Deletions require comparing against previous snapshot or using cancel status.
        window_start = min_start_time or (
            datetime.now(timezone.utc) - timedelta(days=30)
        ).isoformat()

        # Placeholder yield — replace with real API calls once token is available.
        # Example shape (to be filled with actual data):
        yield {
            "uri": "https://api.calendly.com/scheduled_events/example",
            "name": "Example Meeting",
            "start_time": window_start,
            "invitee_questions": "What is the topic? | Project update",
            "notes": "Starter connector placeholder",
            "status": "active",
        }
        logger.info("Calendly: starter sync completed (placeholder data).")

    @dlt.source(name=CALENDLY_SOURCE_NAME)
    def _calendly():
        return calendly_events

    source = _calendly()
    setattr(source, DOCUMENT_SOURCE_ATTR, CALENDLY_SOURCE_NAME)
    return source
