"""DLT source for Calendly events, invitees, and event types.

Fetches Calendly data and yields it as a dlt resource for cognee's ingestion pipeline.
"""

import os
from typing import Any

import dlt
import requests

from cognee.shared.logging_utils import get_logger
from cognee.tasks.ingestion.dlt_utils import DOCUMENT_SOURCE_ATTR

logger = get_logger("calendly_connector")

CALENDLY_TABLE_NAME = "calendly_events"
CALENDLY_SOURCE_NAME = "calendly"
CALENDLY_API_BASE = "https://api.calendly.com/v2"

_MAX_RETRIES = 5

_EXTRA_HINT = (
    'The Calendly connector requires the "calendly" extra: pip install "cognee[calendly]" '
    "(provides dlt and requests)."
)


def _get_auth_headers(token: str | None = None) -> dict[str, str]:
    """Build auth headers for Calendly API."""
    resolved_token = token or os.environ.get("CALENDLY_API_KEY")
    if not resolved_token:
        raise ValueError(
            "Calendly API token required. Set CALENDLY_API_KEY env var or pass token= argument."
        )
    return {
        "Authorization": f"Bearer {resolved_token}",
        "Content-Type": "application/json",
    }


def _calendly_get(path: str, headers: dict[str, str], params: dict | None = None) -> dict:
    """Make a GET request to Calendly API with retry logic."""
    url = f"{CALENDLY_API_BASE}/{path}"
    for attempt in range(_MAX_RETRIES):
        response = requests.get(url, headers=headers, params=params or {}, timeout=30)
        if response.status_code == 429:
            retry_after = int(response.headers.get("Retry-After", 2))
            logger.warning(f"Rate limited by Calendly, waiting {retry_after}s")
            import time
            time.sleep(retry_after)
            continue
        response.raise_for_status()
        return response.json()
    raise RuntimeError(f"Max retries exceeded for {path}")


def calendly_source(
    token: str | None = None,
    user_uri: str | None = None,
    organization_uri: str | None = None,
    min_start_time: str | None = None,
    max_start_time: str | None = None,
    status: str = "active",
):
    """Create a dlt source that yields Calendly events as documents.

    Args:
        token: Calendly Personal Access Token. Falls back to ``CALENDLY_API_KEY``.
        user_uri: Calendly user URI (e.g. ``https://api.calendly.com/users/ABC123``).
            Falls back to ``CALENDLY_USER_URI``.
        organization_uri: Calendly organization URI. Falls back to
            ``CALENDLY_ORG_URI``.
        min_start_time: Only include events starting after this ISO 8601 timestamp.
        max_start_time: Only include events starting before this ISO 8601 timestamp.
        status: Event status filter. One of ``active``, ``canceled``. Default ``active``.

    Returns:
        A dlt source suitable for ``cognee.add(...)`` / ``cognee.remember(...)``.
    """
    try:
        import dlt
    except ImportError as exc:
        raise ImportError(_EXTRA_HINT) from exc

    headers = _get_auth_headers(token)

    resolved_user = user_uri or os.environ.get("CALENDLY_USER_URI")
    resolved_org = organization_uri or os.environ.get("CALENDLY_ORG_URI")

    @dlt.resource(
        name=CALENDLY_TABLE_NAME,
        write_disposition="replace",
        primary_key="uri",
    )
    def _calendly_events():
        # Get current user if not provided
        if not resolved_user:
            me_data = _calendly_get("users/me", headers)
            resolved_user = me_data["resource"]["uri"]
            logger.info(f"Auto-detected Calendly user: {resolved_user}")

        # Build query params
        params: dict[str, Any] = {"status": status}
        if resolved_user:
            params["user"] = resolved_user
        if resolved_org:
            params["organization"] = resolved_org
        if min_start_time:
            params["min_start_time"] = min_start_time
        if max_start_time:
            params["max_start_time"] = max_start_time

        # Fetch scheduled events
        events_data = _calendly_get("scheduled_events", headers, params)
        events = events_data.get("collection", [])

        for event in events:
            # Fetch invitees for each event
            event_uuid = event["uri"].split("/")[-1]
            invitees = []
            try:
                invitees_data = _calendly_get(
                    f"scheduled_events/{event_uuid}/invitees", headers
                )
                invitees = invitees_data.get("collection", [])
            except Exception as e:
                logger.warning(f"Failed to fetch invitees for event {event_uuid}: {e}")

            # Build attendee list
            attendees = []
            for inv in invitees:
                attendees.append({
                    "name": inv.get("name", ""),
                    "email": inv.get("email", ""),
                    "questions": inv.get("questions_and_answers", []),
                    "timezone": inv.get("timezone", ""),
                })

            # Yield structured document
            yield {
                "uri": event["uri"],
                "name": event.get("name", ""),
                "start_time": event.get("start_time", ""),
                "end_time": event.get("end_time", ""),
                "status": event.get("status", ""),
                "location": event.get("location", {}),
                "event_type": event.get("event_type", ""),
                "attendees": attendees,
                "cancelled": event.get("cancelled", False),
                "created_at": event.get("created_at", ""),
                "updated_at": event.get("updated_at", ""),
                DOCUMENT_SOURCE_ATTR: CALENDLY_SOURCE_NAME,
            }

    return _calendly_events
