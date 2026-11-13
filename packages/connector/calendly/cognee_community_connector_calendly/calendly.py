"""DLT source for Calendly events (full-snapshot sync + forget-on-delete).

Fetches Calendly scheduled events and their invitee questions and answers,
then formats them as markdown documents for cognee's ingestion pipeline.

Unlike the relational dlt path (SQL/CSV), Calendly events are ingested as
*normal documents*: the source declares ``cognee_document_source = "calendly"``,
so ``resolve_dlt_sources`` tags each row ``external_metadata["source"] = "calendly"``
(not ``"dlt"``). ``is_dlt_sourced`` therefore returns False and each event flows
through the standard cognify entity-extraction pipeline — the right treatment
for scheduled meetings, attendee notes, and invitee responses — instead of the
deterministic dlt-row schema-context path.

The source defaults to a full snapshot: ``write_disposition="replace"`` rewrites
staging with exactly the events currently active/scheduled. Deletions and
cancellations propagate cleanly — an event deleted or cancelled upstream drops
out of the active snapshot and cognee's existing ``orphan_cleanup`` removes it
from the knowledge graph and vector stores.

Watch out:
Invitee question responses carry the core context (e.g., project details, agendas,
business goals). The calendar slot alone says very little. The connector
fetches invitees and formats their questions and responses directly into the document.
"""

from __future__ import annotations

import hashlib
import math
import os
import re
import time
from typing import Any

import httpx
from cognee.shared.logging_utils import get_logger
from cognee.tasks.ingestion.dlt_utils import DOCUMENT_SOURCE_ATTR

logger = get_logger("calendly_connector")

CALENDLY_TABLE_NAME = "calendly_events"
CALENDLY_SOURCE_NAME = "calendly"

_BASE_URL = "https://api.calendly.com"
_MAX_RETRIES = 5
_BASE_BACKOFF = 1.0


def _extract_uuid_from_uri(uri: str) -> str:
    """Extract UUID from a Calendly resource URI."""
    match = re.search(
        r"([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})",
        uri,
    )
    if match:
        return match.group(1)
    return uri.rstrip("/").split("/")[-1]


def _get_retry_delay(response: httpx.Response | None, attempt: int) -> float:
    """Calculate exponential retry delay honoring Retry-After headers."""
    if response is not None and "retry-after" in response.headers:
        try:
            delay = float(response.headers["retry-after"])
            if math.isfinite(delay) and delay >= 0:
                return delay
        except (ValueError, TypeError):
            pass
    return _BASE_BACKOFF * (2**attempt)


class CalendlyClient:
    """Synchronous HTTP client for the Calendly v2 REST API."""

    def __init__(
        self,
        api_key: str | None = None,
        base_url: str = _BASE_URL,
        transport: httpx.BaseTransport | None = None,
    ):
        token = api_key or os.getenv("CALENDLY_API_KEY") or os.getenv("CALENDLY_ACCESS_TOKEN")
        if not token:
            raise ValueError(
                "Calendly API key required. Pass api_key or set CALENDLY_API_KEY / "
                "CALENDLY_ACCESS_TOKEN."
            )
        self.api_key = token.strip()
        self.base_url = base_url.rstrip("/")
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
            "User-Agent": "cognee-community-connector-calendly/0.1.0",
        }
        self.client = httpx.Client(
            headers=headers,
            timeout=30.0,
            transport=transport,
        )

    def _request(
        self,
        method: str,
        path: str,
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        url = f"{self.base_url}{path}" if path.startswith("/") else path
        for attempt in range(_MAX_RETRIES):
            try:
                resp = self.client.request(method, url, params=params)
                if resp.status_code in (429, 500, 502, 503, 504):
                    delay = _get_retry_delay(resp, attempt)
                    logger.warning(
                        "Calendly API %s returned %s, retrying in %.2fs",
                        url,
                        resp.status_code,
                        delay,
                    )
                    time.sleep(delay)
                    continue
                resp.raise_for_status()
                return resp.json()
            except (httpx.TransportError, httpx.NetworkError) as err:
                if attempt == _MAX_RETRIES - 1:
                    raise
                delay = _get_retry_delay(None, attempt)
                logger.warning(
                    "Network error accessing %s (%s), retrying in %.2fs",
                    url,
                    err,
                    delay,
                )
                time.sleep(delay)

        raise RuntimeError(f"Exceeded max retries calling Calendly API: {url}")

    def get_current_user(self) -> dict[str, Any]:
        """Fetch current authenticated user profile and organization info."""
        data = self._request("GET", "/users/me")
        return data.get("resource", {})

    def list_scheduled_events(
        self,
        user_uri: str | None = None,
        organization_uri: str | None = None,
        min_start_time: str | None = None,
        max_start_time: str | None = None,
        status: str = "active",
        count: int = 100,
        page_token: str | None = None,
    ) -> dict[str, Any]:
        """List scheduled events with optional date and status filters."""
        params: dict[str, Any] = {"count": count}
        if user_uri:
            params["user"] = user_uri
        if organization_uri:
            params["organization"] = organization_uri
        if min_start_time:
            params["min_start_time"] = min_start_time
        if max_start_time:
            params["max_start_time"] = max_start_time
        if status:
            params["status"] = status
        if page_token:
            params["page_token"] = page_token

        return self._request("GET", "/scheduled_events", params=params)

    def list_event_invitees(
        self,
        event_uuid: str,
        count: int = 100,
        page_token: str | None = None,
    ) -> list[dict[str, Any]]:
        """Fetch all invitees and their question responses for an event."""
        invitees: list[dict[str, Any]] = []
        next_token = page_token

        while True:
            params: dict[str, Any] = {"count": count}
            if next_token:
                params["page_token"] = next_token

            data = self._request("GET", f"/scheduled_events/{event_uuid}/invitees", params=params)
            collection = data.get("collection", [])
            invitees.extend(collection)

            pagination = data.get("pagination", {})
            next_token = pagination.get("next_page_token")
            if not next_token:
                break

        return invitees

    def close(self) -> None:
        """Close the underlying HTTP client."""
        self.client.close()


def _event_to_document(
    event: dict[str, Any],
    invitees: list[dict[str, Any]],
) -> dict[str, Any]:
    """Convert a scheduled event and its invitees to a structured markdown document."""
    uri = event.get("uri", "")
    event_id = _extract_uuid_from_uri(uri)
    name = event.get("name", "Scheduled Event")
    status = event.get("status", "unknown")
    start_time = event.get("start_time", "N/A")
    end_time = event.get("end_time", "N/A")
    created_at = event.get("created_at", "N/A")
    updated_at = event.get("updated_at", "N/A")

    location_info = event.get("location") or {}
    loc_str = "None specified"
    if isinstance(location_info, dict):
        loc_type = location_info.get("type", "")
        loc_url = location_info.get("join_url") or location_info.get("location", "")
        if loc_url:
            loc_str = f"{loc_type.capitalize()} ({loc_url})" if loc_type else loc_url
        elif loc_type:
            loc_str = loc_type.capitalize()
    elif isinstance(location_info, str):
        loc_str = location_info

    # Host & event memberships
    memberships = event.get("event_memberships", [])
    hosts = []
    for m in memberships:
        h_name = m.get("user_name") or m.get("user_email") or "Host"
        h_email = m.get("user_email", "")
        hosts.append(f"{h_name} <{h_email}>" if h_email else h_name)
    hosts_str = ", ".join(hosts) if hosts else "N/A"

    doc_lines = [
        f"# Scheduled Event: {name}",
        "",
        f"- **Event ID**: {event_id}",
        f"- **Event URI**: {uri}",
        f"- **Status**: {status}",
        f"- **Start Time**: {start_time}",
        f"- **End Time**: {end_time}",
        f"- **Location**: {loc_str}",
        f"- **Host(s)**: {hosts_str}",
        f"- **Created At**: {created_at}",
        f"- **Updated At**: {updated_at}",
    ]

    meeting_notes = event.get("meeting_notes_plain") or event.get("meeting_notes")
    if meeting_notes:
        doc_lines.extend(["", "## Meeting Notes & Agenda", "", meeting_notes.strip()])

    doc_lines.extend(["", f"## Invitees ({len(invitees)})"])

    for idx, inv in enumerate(invitees, start=1):
        inv_name = inv.get("name", "Invitee")
        inv_email = inv.get("email", "unknown")
        inv_status = inv.get("status", "active")
        inv_tz = inv.get("timezone", "N/A")

        doc_lines.extend(
            [
                "",
                f"### Invitee {idx}: {inv_name} <{inv_email}>",
                f"- **Status**: {inv_status}",
                f"- **Timezone**: {inv_tz}",
            ]
        )

        cancellation = inv.get("cancellation")
        if cancellation and isinstance(cancellation, dict):
            canceled_by = cancellation.get("canceled_by", "Unknown")
            reason = cancellation.get("reason") or "No reason provided"
            doc_lines.append(f"- **Cancellation**: Canceled by {canceled_by} ({reason})")

        qas = inv.get("questions_and_answers", [])
        if qas:
            doc_lines.extend(["- **Questions & Responses**:"])
            for qa in qas:
                q_text = qa.get("question", "").strip()
                a_text = qa.get("answer", "").strip()
                doc_lines.append(f"  - **Q: {q_text}**")
                doc_lines.append(f"    A: {a_text}")

    full_text = "\n".join(doc_lines)
    content_hash = hashlib.sha256(full_text.encode("utf-8")).hexdigest()

    return {
        "id": f"calendly:{event_id}",
        "data_id": f"calendly:{content_hash}",
        "name": name,
        "text": full_text,
        "status": status,
        "start_time": start_time,
        "end_time": end_time,
        "created_at": created_at,
        "updated_at": updated_at,
        "external_metadata": {
            "source": CALENDLY_SOURCE_NAME,
            "event_uri": uri,
            "event_id": event_id,
            "invitee_count": len(invitees),
        },
    }


def calendly_source(
    api_key: str | None = None,
    user_uri: str | None = None,
    organization_uri: str | None = None,
    min_start_time: str | None = None,
    max_start_time: str | None = None,
    status: str = "active",
    include_invitee_qa: bool = True,
    client: CalendlyClient | None = None,
):
    """Create a dlt source yielding Calendly events as markdown documents.

    Args:
        api_key: Calendly Personal Access Token or OAuth Bearer token.
        user_uri: User URI to scope scheduled events. If omitted and organization_uri
            is also omitted, defaults to the authenticated user's URI.
        organization_uri: Organization URI to scope events across an entire org.
        min_start_time: ISO8601 timestamp cutoff for events.
        max_start_time: Optional upper bound ISO8601 cutoff.
        status: Filter events by status: 'active', 'canceled'.
        include_invitee_qa: Whether to fetch invitee question responses.
        client: Optional preconfigured CalendlyClient instance.
    """
    import dlt

    calendly_client = client or CalendlyClient(api_key=api_key)

    @dlt.resource(
        name=CALENDLY_TABLE_NAME,
        write_disposition="replace",
    )
    def scheduled_events_resource():
        nonlocal user_uri, organization_uri
        if not user_uri and not organization_uri:
            try:
                me = calendly_client.get_current_user()
                user_uri = me.get("uri")
            except Exception as exc:
                logger.warning("Could not auto-resolve current user URI: %s", exc)

        # Check incremental watermark cursor stored in dlt state
        state = dlt.current.resource_state()
        watermark = state.get("last_min_start_time")
        effective_min_start = min_start_time or watermark

        next_page_token: str | None = None
        max_seen_start_time = effective_min_start

        while True:
            resp = calendly_client.list_scheduled_events(
                user_uri=user_uri,
                organization_uri=organization_uri,
                min_start_time=effective_min_start,
                max_start_time=max_start_time,
                status=status,
                page_token=next_page_token,
            )

            collection = resp.get("collection", [])
            for event in collection:
                e_start = event.get("start_time")
                if e_start and (max_seen_start_time is None or e_start > max_seen_start_time):
                    max_seen_start_time = e_start

                invitees: list[dict[str, Any]] = []
                if include_invitee_qa:
                    e_uri = event.get("uri", "")
                    e_uuid = _extract_uuid_from_uri(e_uri)
                    try:
                        invitees = calendly_client.list_event_invitees(e_uuid)
                    except Exception as err:
                        logger.warning("Failed to fetch invitees for %s: %s", e_uuid, err)

                yield _event_to_document(event, invitees)

            pagination = resp.get("pagination", {})
            next_page_token = pagination.get("next_page_token")
            if not next_page_token:
                break

        if max_seen_start_time:
            state["last_min_start_time"] = max_seen_start_time

    @dlt.source(name=CALENDLY_SOURCE_NAME)
    def source():
        return scheduled_events_resource

    created_source = source()
    created_source.cognee_document_source = CALENDLY_SOURCE_NAME
    setattr(created_source, DOCUMENT_SOURCE_ATTR, CALENDLY_SOURCE_NAME)
    return created_source
