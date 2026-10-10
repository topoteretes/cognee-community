"""DLT source for Cal.com bookings and events (full-snapshot sync + forget-on-delete).

Fetches Cal.com scheduled bookings, attendee details, and custom questionnaire responses,
then formats them as structured markdown documents for cognee's ingestion pipeline.

Unlike relational dlt sources, Cal.com bookings are ingested as *normal documents*:
the source declares ``DOCUMENT_SOURCE_ATTR = "calcom"``, so ``resolve_dlt_sources`` tags each row
``external_metadata["source"] = "calcom"`` (not ``"dlt"``). Each event flows through the standard
cognify entity-extraction pipeline into Cognee's knowledge graph.

The source defaults to full snapshot replacement: ``write_disposition="replace"`` rewrites
staging with the bookings matching active criteria. Deleted or cancelled meetings drop out
of the active snapshot and cognee's existing ``orphan_cleanup`` purges them from the graph.
"""

from __future__ import annotations

import hashlib
import math
import os
import time
from typing import Any, Iterable

import httpx
from cognee.shared.logging_utils import get_logger
from cognee.tasks.ingestion.dlt_utils import DOCUMENT_SOURCE_ATTR

logger = get_logger("calcom_connector")

CALCOM_TABLE_NAME = "calcom_bookings"
DEFAULT_BASE_URL = "https://api.cal.com/v1"


def get_retry_delay(response: httpx.Response, attempt: int, base_delay: float = 1.0) -> float:
    """Calculate backoff delay from Retry-After header or exponential backoff."""
    retry_after = response.headers.get("Retry-After")
    if retry_after:
        try:
            delay = float(retry_after)
            if math.isfinite(delay) and delay >= 0:
                return delay
        except (ValueError, TypeError):
            pass
    return base_delay * (2**attempt)


class CalComClient:
    """HTTP client for Cal.com API with exponential backoff on HTTP 429 and 5xx."""

    def __init__(
        self,
        api_key: str | None = None,
        base_url: str = DEFAULT_BASE_URL,
        transport: httpx.BaseTransport | None = None,
        timeout: float = 30.0,
        max_retries: int = 3,
    ) -> None:
        self.api_key = api_key or os.getenv("CALCOM_API_KEY")
        if not self.api_key:
            raise ValueError(
                "Cal.com API key is required. Set CALCOM_API_KEY env var or pass api_key."
            )
        self.base_url = (base_url or DEFAULT_BASE_URL).rstrip("/")
        self.max_retries = max_retries
        self.client = httpx.Client(
            base_url=self.base_url,
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
                "User-Agent": "cognee-community-connector-calcom/0.1.0",
            },
            transport=transport,
            timeout=timeout,
        )

    def close(self) -> None:
        self.client.close()

    def __enter__(self) -> CalComClient:
        return self

    def __exit__(self, *args: Any) -> None:
        self.close()

    def get_with_retry(self, endpoint: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        """Execute GET request with retry backoff for rate limits and server errors."""
        url = endpoint if endpoint.startswith("http") else f"{self.base_url}/{endpoint.lstrip('/')}"
        query_params = dict(params or {})
        # Support apiKey query parameter for Cal.com endpoints that expect it
        if "apiKey" not in query_params:
            query_params["apiKey"] = self.api_key

        last_exc: Exception | None = None
        for attempt in range(self.max_retries + 1):
            try:
                response = self.client.get(url, params=query_params)
                if response.status_code == 429 or response.status_code >= 500:
                    if attempt == self.max_retries:
                        response.raise_for_status()
                    delay = get_retry_delay(response, attempt)
                    logger.warning(
                        "Cal.com request to %s returned %d. Backing off for %.2fs (attempt %d/%d)",
                        url,
                        response.status_code,
                        delay,
                        attempt + 1,
                        self.max_retries,
                    )
                    time.sleep(delay)
                    continue

                response.raise_for_status()
                return response.json()
            except (httpx.NetworkError, httpx.TimeoutException) as exc:
                last_exc = exc
                if attempt == self.max_retries:
                    raise
                delay = 1.0 * (2**attempt)
                logger.warning(
                    "Network error connecting to Cal.com: %s. Retrying in %.2fs (attempt %d/%d)",
                    exc,
                    delay,
                    attempt + 1,
                    self.max_retries,
                )
                time.sleep(delay)

        if last_exc:
            raise last_exc
        raise RuntimeError("Unexpected failure in get_with_retry")

    def list_bookings(
        self,
        status: str | None = None,
        after: str | None = None,
        page: int = 1,
        take: int = 100,
    ) -> list[dict[str, Any]]:
        """Fetch bookings page from Cal.com."""
        params: dict[str, Any] = {"page": page, "take": take}
        if status:
            params["status"] = status
        if after:
            params["after"] = after

        data = self.get_with_retry("bookings", params=params)
        if isinstance(data, dict):
            bookings = data.get("bookings")
            if isinstance(bookings, list):
                return bookings
            return data.get("data", [])
        if isinstance(data, list):
            return data
        return []


def booking_to_document(booking: dict[str, Any]) -> dict[str, Any]:
    """Convert a Cal.com booking payload into a rich Markdown document for cognify."""
    booking_id = str(booking.get("id", ""))
    title = booking.get("title") or "Scheduled Booking"
    description = booking.get("description") or ""
    status = booking.get("status") or "ACCEPTED"
    start_time = booking.get("startTime") or ""
    end_time = booking.get("endTime") or ""
    location = booking.get("location") or booking.get("meetingUrl") or "Online / Remote"
    cancellation_reason = booking.get("cancellationReason") or ""

    user = booking.get("user") or {}
    host_name = user.get("name") or "Organizer"
    host_email = user.get("email") or ""

    event_type = booking.get("eventType") or {}
    event_title = event_type.get("title") or "General Meeting"

    attendees = booking.get("attendees") or []
    attendee_lines: list[str] = []
    for att in attendees:
        name = att.get("name") or "Attendee"
        email = att.get("email") or ""
        tz = att.get("timeZone") or ""
        tz_str = f" [{tz}]" if tz else ""
        attendee_lines.append(f"- {name} ({email}){tz_str}")

    responses = booking.get("responses") or {}
    response_lines: list[str] = []
    if isinstance(responses, dict):
        for question, answer in responses.items():
            if question not in ("name", "email", "notes", "guests"):
                response_lines.append(f"- **{question}**: {answer}")
    elif isinstance(responses, list):
        for resp in responses:
            if isinstance(resp, dict):
                q = resp.get("label") or resp.get("question") or "Question"
                a = resp.get("value") or resp.get("response") or ""
                response_lines.append(f"- **{q}**: {a}")

    doc_parts = [
        f"# Meeting: {title}",
        "",
        f"- **Status**: {status}",
        f"- **Host**: {host_name} ({host_email})" if host_email else f"- **Host**: {host_name}",
        f"- **Event Type**: {event_title}",
        f"- **Start Time**: {start_time}",
        f"- **End Time**: {end_time}",
        f"- **Location**: {location}",
    ]

    if description:
        doc_parts.extend(["", "## Description & Agenda", description])

    if attendee_lines:
        doc_parts.extend(["", "## Attendees", *attendee_lines])

    if response_lines:
        doc_parts.extend(["", "## Booking Questions & Responses", *response_lines])

    if cancellation_reason:
        doc_parts.extend(["", "## Cancellation Reason", cancellation_reason])

    markdown_text = "\n".join(doc_parts)
    raw_hash = hashlib.sha256(f"{booking_id}_{start_time}_{status}".encode("utf-8")).hexdigest()

    return {
        "id": booking_id,
        "title": title,
        "status": status,
        "start_time": start_time,
        "end_time": end_time,
        "host_email": host_email,
        "text": markdown_text,
        "content": markdown_text,
        "raw_hash": raw_hash,
        "metadata": {
            "source": "calcom",
            "booking_id": booking_id,
            "status": status,
            "start_time": start_time,
            "event_type": event_title,
        },
    }


def fetch_calcom_bookings(
    api_key: str | None = None,
    base_url: str = DEFAULT_BASE_URL,
    status_filter: str | None = "ACCEPTED",
    incremental: bool = False,
    page_size: int = 100,
    transport: httpx.BaseTransport | None = None,
) -> Iterable[dict[str, Any]]:
    """Yield Cal.com booking documents for dlt ingestion."""
    import dlt

    state = dlt.current.resource_state() if incremental else {}
    last_watermark = state.get("last_updated_after") if incremental else None

    client = CalComClient(
        api_key=api_key,
        base_url=base_url,
        transport=transport,
    )

    page = 1
    max_updated = last_watermark

    try:
        while True:
            bookings = client.list_bookings(
                status=status_filter,
                after=last_watermark,
                page=page,
                take=page_size,
            )
            if not bookings:
                break

            for booking in bookings:
                updated_at = (
                    booking.get("updatedAt") or booking.get("startTime") or booking.get("createdAt")
                )
                if updated_at and (max_updated is None or updated_at > max_updated):
                    max_updated = updated_at

                yield booking_to_document(booking)

            if len(bookings) < page_size:
                break
            page += 1

        if incremental and max_updated:
            state["last_updated_after"] = max_updated
    finally:
        client.close()


def calcom_source(
    api_key: str | None = None,
    base_url: str = DEFAULT_BASE_URL,
    status_filter: str | None = "ACCEPTED",
    incremental: bool = False,
    page_size: int = 100,
    transport: httpx.BaseTransport | None = None,
) -> Any:
    """Create a dlt source for Cal.com meeting bookings."""
    import dlt

    @dlt.resource(
        name=CALCOM_TABLE_NAME,
        write_disposition="merge" if incremental else "replace",
        primary_key="id",
    )
    def bookings() -> Iterable[dict[str, Any]]:
        yield from fetch_calcom_bookings(
            api_key=api_key,
            base_url=base_url,
            status_filter=status_filter,
            incremental=incremental,
            page_size=page_size,
            transport=transport,
        )

    @dlt.source(name="calcom")
    def source() -> Any:
        return bookings

    created_source = source()
    setattr(created_source, DOCUMENT_SOURCE_ATTR, "calcom")
    return created_source
