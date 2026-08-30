"""DLT source for Calendly events (full-snapshot sync + forget-on-delete).

Fetches Calendly scheduled events with invitee information and renders them to
markdown, then yields them as a dlt resource for cognee's ingestion pipeline.

Unlike the relational dlt path (SQL/CSV), Calendly events are ingested as *normal
documents*: the source declares ``cognee_document_source = "calendly"``, so
``resolve_dlt_sources`` tags each row ``external_metadata["source"] = "calendly"``
(not ``"dlt"``). ``is_dlt_sourced`` therefore returns False and each event flows
through the standard cognify entity-extraction pipeline — the right treatment
for prose — instead of the deterministic dlt-row schema-context path.

The source is a full snapshot: ``write_disposition="replace"`` rewrites staging
with exactly the events currently visible to the integration each run. Deleted
events simply drop out of the snapshot and cognee's existing ``orphan_cleanup``
removes them from the graph and vector stores. Unchanged events keep a stable
content-hash ``data_id``, so they are not re-ingested or re-cognified. Events
use incremental sync with ``min_start_time`` to efficiently fetch only changed
data since the last sync.
"""

import os
import time
from datetime import datetime, timedelta
from typing import Any, Optional

from cognee.shared.logging_utils import get_logger
from cognee.tasks.ingestion.dlt_utils import DOCUMENT_SOURCE_ATTR

logger = get_logger("calendly_connector")

# dlt resource / staging-table name for Calendly events.
CALENDLY_TABLE_NAME = "calendly_events"
CALENDLY_SOURCE_NAME = "calendly"
# Calendly API v1 base URL
CALENDLY_API_URL = "https://api.calendly.com"

# Retry budget for rate-limited / transient Calendly API responses.
_MAX_RETRIES = 5

_EXTRA_HINT = (
    'The Calendly connector requires the "calendly" extra: pip install "cognee[calendly]" '
    "(provides dlt and requests)."
)


def calendly_source(
    token: str | None = None,
    min_start_time: Optional[str] = None,
    client: Any = None,
) -> Any:
    """Create a dlt source that yields Calendly events as markdown documents.

    Args:
        token: Calendly personal access token. Falls back to ``CALENDLY_API_KEY``.
        min_start_time: ISO 8601 datetime string for incremental sync. Only events
            starting at or after this time are fetched. If omitted, fetches all
            events visible to the token.
        client: Pre-built ``requests.Session`` (mainly a test-injection point);
            when omitted one is built from the token above.

    Returns:
        A dlt source suitable for ``cognee.add(...)`` / ``cognee.remember(...)``.
    """
    try:
        import dlt
    except ImportError as exc:
        raise ImportError(_EXTRA_HINT) from exc

    if client is None:
        try:
            import requests
        except ImportError as exc:
            raise ImportError(_EXTRA_HINT) from exc

        resolved_token = token or os.environ.get("CALENDLY_API_KEY")
        if not resolved_token:
            raise ValueError(
                "Calendly personal access token required: pass token= or set CALENDLY_API_KEY."
            )
        
        client = requests.Session()
        client.headers.update({"Authorization": f"Bearer {resolved_token}"})

    @dlt.resource(
        name=CALENDLY_TABLE_NAME, primary_key="uri", write_disposition="replace"
    )
    def calendly_events():
        # Full-snapshot sync: each run replaces staging with exactly the events
        # currently visible to the token. Deleted events drop out of the API
        # response (Calendly doesn't return them), so they fall out of staging
        # and cognee's orphan_cleanup then forgets them from the graph + vector
        # stores. Unchanged events keep a stable content-hash data_id, so they
        # are not re-ingested/re-cognified.
        #
        # A render/fetch error is NOT swallowed: because staging is authoritative
        # (replace), an event missing from a partial snapshot would be forgotten
        # as if deleted. Letting the error abort the run leaves staging — and
        # memory — untouched, which is the safe failure.
        count = 0
        for event in _iter_events(client, min_start_time):
            count += 1
            yield _event_to_row(client, event)
        logger.info("Calendly: synced %d event(s).", count)

    @dlt.source(name=CALENDLY_SOURCE_NAME)
    def _calendly():
        return calendly_events

    source = _calendly()
    # Opt into the document ingestion path (event → text document → cognify).
    # resolve_dlt_sources reads this marker; it never imports this connector.
    setattr(source, DOCUMENT_SOURCE_ATTR, CALENDLY_SOURCE_NAME)
    return source


# ---------------------------------------------------------------------------
# Calendly API helpers (module-private)
# ---------------------------------------------------------------------------


def _request(client, method: str, path: str, **kwargs) -> dict:
    """Call a Calendly API endpoint, retrying rate-limit / transient errors.

    The Calendly API enforces rate limits, so transient errors are retried
    with backoff. Rate-limit (429), server (5xx), timeout, and network errors
    are retried; permanent errors (auth, not-found) propagate.
    """
    url = f"{CALENDLY_API_URL}{path}"
    for attempt in range(_MAX_RETRIES):
        try:
            if method.upper() == "GET":
                response = client.get(url, **kwargs)
            else:
                response = client.request(method, url, **kwargs)
            
            response.raise_for_status()
            return response.json()
        except Exception as exc:
            if attempt == _MAX_RETRIES - 1 or not _is_transient(exc):
                raise
            delay = _retry_after(attempt)
            logger.warning(
                "Calendly: %s — retrying in %.1fs (%d/%d).",
                exc,
                delay,
                attempt + 1,
                _MAX_RETRIES,
            )
            time.sleep(delay)


def _is_transient(exc: Exception) -> bool:
    """True for rate-limit / server / timeout / network errors worth retrying."""
    try:
        import requests
    except ImportError:
        return False

    if isinstance(exc, requests.Timeout):
        return True
    if isinstance(exc, requests.ConnectionError):
        return True
    if isinstance(exc, requests.HTTPError):
        status = getattr(exc.response, "status_code", None)
        return status in (429, 500, 502, 503, 504)
    return False


def _retry_after(attempt: int) -> float:
    """Seconds to wait before retrying: exponential backoff."""
    return float(2**attempt)


def _iter_events(client, min_start_time: Optional[str]):
    """Yield raw Calendly event objects for the configured scope."""
    page_token = None
    params = {
        "state": "active",
        "sort": "start_time:asc",
        "count": 100,
    }
    
    if min_start_time:
        params["min_start_time"] = min_start_time
    
    while True:
        if page_token:
            params["page_token"] = page_token
        
        response = _request(client, "GET", "/scheduled_events", params=params)
        
        for event in response.get("collection", []):
            yield event
        
        # Check for pagination
        page_token = response.get("pagination", {}).get("next_page_token")
        if not page_token:
            break


def _event_to_row(client, event: dict) -> dict:
    """Flatten a Calendly event + its invitees into a document row.

    Combines event details, invitee information, and invitee responses
    into a markdown document.
    """
    event_uri = event.get("uri")
    
    # Fetch invitees for this event
    invitees_text = _fetch_invitees_text(client, event_uri)
    
    return {
        "uri": event_uri,
        "event_id": event.get("name"),
        "title": event.get("name"),
        "status": event.get("status"),
        "content": _render_event(event, invitees_text),
    }


def _fetch_invitees_text(client, event_uri: str) -> str:
    """Fetch invitees for an event and render them to markdown."""
    try:
        # Extract event UUID from URI (format: https://api.calendly.com/scheduled_events/{id})
        event_id = event_uri.split("/")[-1]
        response = _request(client, "GET", f"/scheduled_events/{event_id}/invitees")
        
        lines = []
        for invitee in response.get("collection", []):
            invitee_text = _render_invitee(invitee)
            if invitee_text:
                lines.append(invitee_text)
        
        return "\n".join(lines) if lines else ""
    except Exception as exc:
        logger.warning("Calendly: failed to fetch invitees for %s: %s", event_uri, exc)
        return ""


def _render_event(event: dict, invitees_text: str) -> str:
    """Render a Calendly event to markdown."""
    lines = []
    
    # Event details
    name = event.get("name")
    if name:
        lines.append(f"# {name}")
    
    # Status
    status = event.get("status")
    if status:
        lines.append(f"**Status:** {status}")
    
    # Start and end times
    start_time = event.get("start_time")
    end_time = event.get("end_time")
    if start_time:
        lines.append(f"**Start Time:** {start_time}")
    if end_time:
        lines.append(f"**End Time:** {end_time}")
    
    # Event type
    event_type = event.get("event_type")
    if event_type:
        lines.append(f"**Event Type:** {event_type}")
    
    # Description/Notes
    description = event.get("description")
    if description:
        lines.append(f"\n## Notes\n{description}")
    
    # Invitees and their responses
    if invitees_text:
        lines.append(f"\n## Invitees & Responses\n{invitees_text}")
    
    return "\n".join(lines)


def _render_invitee(invitee: dict) -> str:
    """Render a single invitee with their response to markdown."""
    lines = []
    
    name = invitee.get("name")
    email = invitee.get("email")
    
    if name or email:
        name_str = f"{name} ({email})" if email else name or email
        lines.append(f"### {name_str}")
    
    # Status
    status = invitee.get("status")
    if status:
        lines.append(f"- **Status:** {status}")
    
    # Answer to questions (custom questions and their responses)
    questions_and_answers = invitee.get("questions_and_answers", [])
    if questions_and_answers:
        lines.append("- **Q&A:**")
        for qa in questions_and_answers:
            question = qa.get("question", "")
            answer = qa.get("answer", "")
            if question and answer:
                lines.append(f"  - {question}: {answer}")
            elif question:
                lines.append(f"  - {question}: (no answer)")
    
    return "\n".join(lines) if lines else ""
