"""DLT source for Calendly events (sync scheduled events, invitee questions, and notes).

Fetches Calendly scheduled events and their invitees (including questions, answers,
and notes), renders them to structured markdown documents, and yields them as a dlt
resource for cognee's ingestion pipeline.

Unlike the relational dlt path (SQL/CSV), Calendly events are ingested as *normal
documents*: the source declares ``cognee_document_source = "calendly"`` via
``DOCUMENT_SOURCE_ATTR``, so ``resolve_dlt_sources`` tags each row with
``external_metadata["source"] = "calendly"`` (or ``system_metadata["source"] = "calendly"``).
``is_dlt_sourced`` therefore returns False and each meeting document flows through
the standard cognify entity-extraction pipeline (chunking + LLM knowledge-graph extraction).

The source uses full snapshot replace semantics within the queried scope:
``write_disposition="replace"`` rewrites staging with the events currently active and
visible to the integration. Deletions or cancellations propagate cleanly:
- Canceled events (``status == "canceled"``) and events removed upstream are omitted
  from the yielded snapshot.
- As a result, deleted/canceled events drop out of staging and cognee's ``orphan_cleanup``
  purges them from the knowledge graph and vector stores.
- Unchanged events keep a stable content-hash ``id``, preventing unnecessary re-cognition.
"""

import hashlib
import os
import time
from typing import Any
from urllib.parse import urlparse

import requests
from cognee.shared.logging_utils import get_logger
from cognee.tasks.ingestion.dlt_utils import DOCUMENT_SOURCE_ATTR

logger = get_logger("calendly_connector")

# dlt resource / staging-table name for Calendly events.
CALENDLY_TABLE_NAME = "calendly_events"
CALENDLY_SOURCE_NAME = "calendly"
CALENDLY_BASE_URL = "https://api.calendly.com"

# Retry budget for rate-limited (429) / transient Calendly API responses.
_MAX_RETRIES = 5

_EXTRA_HINT = (
    "The Calendly connector requires dlt and requests: "
    'pip install "cognee-community-connector-calendly" or install into your environment.'
)


def _extract_uuid(uri_or_uuid: str | None) -> str:
    """Extract UUID from Calendly URI or return UUID as-is."""
    if not uri_or_uuid:
        return ""
    path = urlparse(uri_or_uuid).path.rstrip("/")
    if path:
        return path.split("/")[-1]
    return uri_or_uuid.rstrip("/").split("/")[-1]


def _is_transient(status_code: int) -> bool:
    """True for HTTP rate-limit (429) or server errors (5xx) worth retrying."""
    return status_code in (429, 500, 502, 503, 504)


def _retry_after(headers: dict[str, str] | Any, attempt: int) -> float:
    """Seconds to wait before retrying: honors Retry-After header, else exponential backoff."""
    if headers:
        header = headers.get("Retry-After") or headers.get("retry-after")
        if header:
            try:
                return float(header)
            except (TypeError, ValueError):
                pass
    return float(2**attempt)


def _request(
    session: requests.Session,
    method: str,
    url: str,
    headers: dict[str, str],
    params: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Execute HTTP request with backoff for 429/5xx responses."""
    for attempt in range(_MAX_RETRIES):
        try:
            resp = session.request(method=method, url=url, headers=headers, params=params)
            if resp.status_code == 200:
                return resp.json()
            if not _is_transient(resp.status_code) or attempt == _MAX_RETRIES - 1:
                resp.raise_for_status()
            delay = _retry_after(resp.headers, attempt)
            logger.warning(
                "Calendly API %s %s returned %d — retrying in %.1fs (%d/%d).",
                method,
                url,
                resp.status_code,
                delay,
                attempt + 1,
                _MAX_RETRIES,
            )
            time.sleep(delay)
        except requests.RequestException as exc:
            status = getattr(getattr(exc, "response", None), "status_code", None)
            if attempt == _MAX_RETRIES - 1 or (status is not None and not _is_transient(status)):
                raise
            delay = 2**attempt
            logger.warning(
                "Calendly API %s %s failed with %s — retrying in %.1fs (%d/%d).",
                method,
                url,
                exc,
                delay,
                attempt + 1,
                _MAX_RETRIES,
            )
            time.sleep(delay)

    return {}


def _get_current_user(session: requests.Session, headers: dict[str, str]) -> dict[str, Any]:
    """Fetch details of the currently authenticated user from /users/me."""
    url = f"{CALENDLY_BASE_URL}/users/me"
    data = _request(session, "GET", url, headers=headers)
    return data.get("resource") or {}


def _paginate(
    session: requests.Session,
    url: str,
    headers: dict[str, str],
    params: dict[str, Any] | None = None,
):
    """Yield items across Calendly keyset pagination (pagination.next_page)."""
    current_url = url
    current_params = params

    while current_url:
        data = _request(session, "GET", current_url, headers=headers, params=current_params)
        collection = data.get("collection") or []
        yield from collection

        pagination = data.get("pagination") or {}
        next_page = pagination.get("next_page")

        # After the initial request, next_page contains full URL including page_token
        if next_page:
            current_url = next_page
            current_params = None
        else:
            break


def _fetch_event_invitees(
    session: requests.Session,
    event_uri_or_uuid: str,
    headers: dict[str, str],
) -> list[dict[str, Any]]:
    """Retrieve all invitees for a given event, including questions & answers."""
    event_uuid = _extract_uuid(event_uri_or_uuid)
    if not event_uuid:
        return []

    url = f"{CALENDLY_BASE_URL}/scheduled_events/{event_uuid}/invitees"
    try:
        return list(_paginate(session, url, headers=headers))
    except Exception as exc:
        logger.warning(
            "Calendly: failed to fetch invitees for event %s: %s",
            event_uuid,
            exc,
        )
        return []


def _format_location(location: dict[str, Any] | None) -> str:
    """Format meeting location / video link cleanly."""
    if not location or not isinstance(location, dict):
        return ""
    loc_type = location.get("type") or ""
    join_url = location.get("join_url") or location.get("location") or ""
    if join_url:
        return f"{loc_type.capitalize()} ({join_url})" if loc_type else join_url
    return loc_type.capitalize() if loc_type else ""


def _build_event_content(
    event: dict[str, Any],
    invitees: list[dict[str, Any]],
) -> str:
    """Assemble meeting metadata, location, description, and invitee Q&A into markdown."""
    name = event.get("name") or "Scheduled Event"
    status = event.get("status") or "active"
    start_time = event.get("start_time") or ""
    end_time = event.get("end_time") or ""
    created_at = event.get("created_at") or ""
    event_uuid = _extract_uuid(event.get("uri"))

    location_str = _format_location(event.get("location"))

    # Event memberships (organizers / hosts)
    memberships = event.get("event_memberships") or []
    hosts: list[str] = []
    for m in memberships:
        if isinstance(m, dict):
            h_name = m.get("user_name") or m.get("user_email")
            if h_name:
                hosts.append(h_name)
    hosts_str = ", ".join(hosts)

    lines = [f"# {name}", ""]
    lines.append(f"- Status: {status}")
    if start_time:
        lines.append(f"- Start Time: {start_time}")
    if end_time:
        lines.append(f"- End Time: {end_time}")
    if created_at:
        lines.append(f"- Created At: {created_at}")
    if hosts_str:
        lines.append(f"- Host(s): {hosts_str}")
    if location_str:
        lines.append(f"- Location: {location_str}")
    if event_uuid:
        lines.append(f"- Event ID: {event_uuid}")

    # Event description / notes (if provided by host)
    meeting_notes = event.get("meeting_notes_plain") or event.get("description")
    if meeting_notes:
        lines.extend(["", "## Meeting Notes", meeting_notes.strip()])

    # Invitees section
    if invitees:
        lines.extend(["", "## Invitees"])
        for inv in invitees:
            inv_name = inv.get("name") or "Unknown"
            inv_email = inv.get("email") or ""
            inv_status = inv.get("status") or ""

            inv_header = f"### {inv_name}"
            if inv_email:
                inv_header += f" ({inv_email})"
            lines.extend(["", inv_header])
            if inv_status:
                lines.append(f"- Invitee Status: {inv_status}")

            timezone = inv.get("timezone")
            if timezone:
                lines.append(f"- Timezone: {timezone}")

            # Invitee Questions & Answers (CRITICAL for search context)
            q_and_a = inv.get("questions_and_answers") or []
            if isinstance(q_and_a, list) and q_and_a:
                lines.extend(["", "#### Questions & Answers"])
                for qa in q_and_a:
                    if isinstance(qa, dict):
                        question = qa.get("question") or "Question"
                        answer = qa.get("answer") or "No answer provided"
                        lines.append(f"**Q: {question}**")
                        lines.append(f"A: {answer}")
                        lines.append("")

            # Additional invitee notes / text
            text_reminder = inv.get("text_reminder_number")
            if text_reminder:
                lines.append(f"- Text Reminder: {text_reminder}")

    return "\n".join(lines).strip()


def _event_to_row(
    event: dict[str, Any],
    invitees: list[dict[str, Any]],
) -> dict[str, Any]:
    """Convert event and invitee details into a cognee document row."""
    event_uri = event.get("uri") or ""
    event_uuid = _extract_uuid(event_uri)
    name = event.get("name") or "Scheduled Event"
    content = _build_event_content(event, invitees)

    # Content hash ensures stable id for unchanged content
    content_hash = hashlib.sha256((event_uuid + content).encode("utf-8")).hexdigest()

    return {
        "id": content_hash,
        "title": name,
        "url": event_uri,
        "content": content,
    }


def calendly_source(
    token: str | None = None,
    user_uri: str | None = None,
    organization_uri: str | None = None,
    min_start_time: str | None = None,
    max_start_time: str | None = None,
    status: str = "active",
    session: requests.Session | None = None,
):
    """Create a dlt source that yields Calendly events and invitees as markdown documents.

    Args:
        token: Calendly Personal Access Token or Bearer token.
            Falls back to ``CALENDLY_API_KEY`` or ``CALENDLY_ACCESS_TOKEN``.
        user_uri: Restrict events to this user URI (e.g. "https://api.calendly.com/users/UUID").
            If omitted and no organization_uri, defaults to the authenticated user from /users/me.
        organization_uri: Restrict events to this organization URI.
        min_start_time: Lower bound (inclusive) for event start time in ISO 8601 format.
            (Used for incremental sync windowing).
        max_start_time: Upper bound (inclusive) for event start time in ISO 8601 format.
        status: Filter events by status. Defaults to ``"active"``. Canceled events are
            automatically excluded so they are purged from cognee memory via orphan cleanup.
        session: Pre-built ``requests.Session`` (useful for testing or custom transports).

    Returns:
        A dlt source suitable for ``cognee.add(...)`` / ``cognee.remember(...)``.
    """
    try:
        import dlt
    except ImportError as exc:
        raise ImportError(_EXTRA_HINT) from exc

    resolved_token = (
        token or os.environ.get("CALENDLY_API_KEY") or os.environ.get("CALENDLY_ACCESS_TOKEN")
    )
    if not resolved_token:
        raise ValueError(
            "Calendly token required: pass token= or set CALENDLY_API_KEY / CALENDLY_ACCESS_TOKEN."
        )

    http_session = session or requests.Session()
    headers = {
        "Authorization": f"Bearer {resolved_token}",
        "Content-Type": "application/json",
    }

    # Resolve user or organization scope if neither was provided
    resolved_user_uri = user_uri
    resolved_org_uri = organization_uri

    if not resolved_user_uri and not resolved_org_uri:
        current_user = _get_current_user(http_session, headers)
        resolved_user_uri = current_user.get("uri")
        if not resolved_user_uri:
            # Fallback to current user organization if available
            resolved_org_uri = current_user.get("current_organization")

    @dlt.resource(name=CALENDLY_TABLE_NAME, primary_key="id", write_disposition="replace")
    def calendly_events():
        # Build query parameters for /scheduled_events
        params: dict[str, Any] = {"status": status}
        if resolved_user_uri:
            params["user"] = resolved_user_uri
        elif resolved_org_uri:
            params["organization"] = resolved_org_uri

        if min_start_time:
            params["min_start_time"] = min_start_time
        if max_start_time:
            params["max_start_time"] = max_start_time

        url = f"{CALENDLY_BASE_URL}/scheduled_events"
        count = 0

        for event in _paginate(http_session, url, headers=headers, params=params):
            event_status = event.get("status")
            # Forget-on-delete / forget-on-cancel:
            # Skip any event that is canceled or not active, so it vanishes from the snapshot
            # and is forgotten from cognee's memory by orphan_cleanup.
            if event_status != "active":
                continue

            event_uri = event.get("uri")
            invitees = _fetch_event_invitees(http_session, event_uri, headers=headers)
            count += 1
            yield _event_to_row(event, invitees)

        logger.info("Calendly: synced %d active event(s).", count)

    @dlt.source(name=CALENDLY_SOURCE_NAME)
    def _calendly():
        return calendly_events

    source = _calendly()
    # Opt into document mode: events flow through cognify entity extraction
    setattr(source, DOCUMENT_SOURCE_ATTR, CALENDLY_SOURCE_NAME)
    return source
