"""Calendly data-source connector for cognee."""

from __future__ import annotations

import hashlib
import os
from collections.abc import Iterator
from typing import Any

CALENDLY_API_BASE = "https://api.calendly.com"


def _headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _request_json(
    client: Any,
    path_or_url: str,
    *,
    token: str,
    params: dict[str, Any] | None = None,
) -> dict[str, Any]:
    url = path_or_url
    if not url.startswith("http"):
        url = f"{CALENDLY_API_BASE}{path_or_url}"

    response = client.get(url, headers=_headers(token), params=params)
    response.raise_for_status()

    payload = response.json()
    if not isinstance(payload, dict):
        raise ValueError(f"Calendly API returned invalid JSON for {url}")

    return payload


def _paginate(
    client: Any,
    path: str,
    *,
    token: str,
    params: dict[str, Any] | None = None,
) -> Iterator[dict[str, Any]]:
    """Yield items from a cursor-paginated Calendly endpoint."""
    current_url = path
    current_params = dict(params or {})

    while current_url:
        payload = _request_json(
            client,
            current_url,
            token=token,
            params=current_params,
        )

        collection = payload.get("collection") or []
        if not isinstance(collection, list):
            raise ValueError("Calendly API collection must be a list")

        for item in collection:
            if isinstance(item, dict):
                yield item

        pagination = payload.get("pagination") or {}
        next_page = pagination.get("next_page")

        if not next_page:
            break

        if str(next_page).startswith("http"):
            current_url = str(next_page)
            current_params = {}
        else:
            current_url = path
            current_params = dict(params or {})
            current_params["page_token"] = next_page


def _current_user_uri(client: Any, *, token: str) -> str:
    payload = _request_json(client, "/users/me", token=token)
    resource = payload.get("resource") or {}

    user_uri = resource.get("uri")
    if not user_uri:
        raise ValueError("Could not determine Calendly user URI")

    return str(user_uri)


def _question_text(question_and_answer: dict[str, Any]) -> str:
    question = question_and_answer.get("question") or "Question"
    answer = question_and_answer.get("answer") or ""
    return f"{question}: {answer}"


def _invitee_to_row(invitee: dict[str, Any], event: dict[str, Any]) -> dict[str, Any]:
    """Convert an invitee and event context into one searchable row."""
    invitee_uri = invitee.get("uri") or invitee.get("email")
    if not invitee_uri:
        raise ValueError("Invitee must contain a URI or email")

    responses = invitee.get("questions_and_answers") or []
    response_lines = [_question_text(item) for item in responses if isinstance(item, dict)]

    text_parts = [
        f"Event: {event.get('name') or 'Calendly event'}",
        f"Start: {event.get('start_time')}" if event.get("start_time") else "",
        f"End: {event.get('end_time')}" if event.get("end_time") else "",
        f"Invitee: {invitee.get('name')}" if invitee.get("name") else "",
        f"Email: {invitee.get('email')}" if invitee.get("email") else "",
        f"Status: {invitee.get('status')}" if invitee.get("status") else "",
    ]

    if response_lines:
        text_parts.append("Invitee responses:")
        text_parts.extend(response_lines)

    text = "\n".join(part for part in text_parts if part)

    content = "|".join(
        [
            str(invitee_uri),
            str(event.get("uri") or ""),
            text,
            str(invitee.get("updated_at") or ""),
            str(event.get("updated_at") or ""),
        ]
    )

    return {
        "id": str(invitee_uri),
        "data_id": hashlib.sha256(content.encode()).hexdigest(),
        "event_uri": event.get("uri"),
        "event_name": event.get("name"),
        "event_type": event.get("event_type"),
        "start_time": event.get("start_time"),
        "end_time": event.get("end_time"),
        "event_status": event.get("status"),
        "invitee_name": invitee.get("name"),
        "invitee_email": invitee.get("email"),
        "status": invitee.get("status"),
        "questions_and_answers": response_lines,
        "text": text,
        "_deleted": False,
    }


def _deleted_row(invitee: dict[str, Any]) -> dict[str, Any]:
    """Build a dlt hard-delete row for a cancelled/deleted invitee."""
    invitee_uri = invitee.get("uri") or invitee.get("email")
    if not invitee_uri:
        raise ValueError("Invitee must contain a URI or email")

    return {
        "id": str(invitee_uri),
        "_deleted": True,
    }


def iter_calendly_rows(
    client: Any,
    *,
    token: str,
    min_start_time: str | None = None,
    event_type_uris: list[str] | None = None,
    user_uri: str | None = None,
) -> Iterator[dict[str, Any]]:
    """Yield Calendly invitee rows and deletion markers.

    Active events are emitted as searchable rows. Cancelled events are emitted
    as hard-delete markers for their invitees so the next sync removes stale
    records from the dlt destination.
    """
    user_uri = user_uri or _current_user_uri(client, token=token)

    selected_types = set(event_type_uris or [])

    base_params: dict[str, Any] = {"user": user_uri}
    if min_start_time:
        base_params["min_start_time"] = min_start_time

    seen_events: set[str] = set()

    for event_status in ("active", "canceled"):
        params = {**base_params, "status": event_status}

        for event in _paginate(
            client,
            "/scheduled_events",
            token=token,
            params=params,
        ):
            event_uri = event.get("uri")
            if not event_uri or str(event_uri) in seen_events:
                continue

            seen_events.add(str(event_uri))

            if selected_types and event.get("event_type") not in selected_types:
                continue

            for invitee in _paginate(
                client,
                f"{event_uri}/invitees",
                token=token,
            ):
                if event_status == "canceled" or event.get("status") == "canceled":
                    yield _deleted_row(invitee)
                else:
                    yield _invitee_to_row(invitee, event)


def calendly_source(
    *,
    token: str | None = None,
    min_start_time: str | None = None,
    event_type_uris: list[str] | None = None,
    incremental: bool = True,
    client: Any | None = None,
):
    """Return a dlt resource for ingesting Calendly data into cognee.

    Incremental mode persists the latest event start time in dlt resource state
    and sends it as Calendly's ``min_start_time`` on subsequent runs.

    Cancelled upstream events emit dlt hard-delete markers, allowing invitee
    records to be removed from the destination on the next synchronization.
    """

    try:
        import dlt
    except ImportError as exc:
        raise ImportError("Install the connector dependencies first.") from exc

    token = token or os.getenv("CALENDLY_API_TOKEN")
    if not token:
        raise ValueError("Pass token=... or set the CALENDLY_API_TOKEN environment variable.")

    if client is None:
        import httpx

        client = httpx.Client(timeout=30.0)

    write_disposition = "merge" if incremental else "replace"

    resource_kwargs: dict[str, Any] = {
        "name": "calendly_invitees",
        "primary_key": "id",
        "write_disposition": write_disposition,
    }

    if incremental:
        resource_kwargs["columns"] = {
            "_deleted": {
                "data_type": "bool",
                "hard_delete": True,
            }
        }

    @dlt.resource(**resource_kwargs)
    def calendly_invitees():
        state = dlt.current.resource_state()

        cursor = min_start_time
        if incremental and cursor is None:
            cursor = state.get("last_start_time")

        latest_start_time = cursor

        for row in iter_calendly_rows(
            client,
            token=token,
            min_start_time=cursor,
            event_type_uris=event_type_uris,
        ):
            if not row.get("_deleted"):
                start_time = row.get("start_time")

                if start_time and (
                    latest_start_time is None or str(start_time) > str(latest_start_time)
                ):
                    latest_start_time = str(start_time)

            yield row

        if incremental and latest_start_time:
            state["last_start_time"] = latest_start_time

    return calendly_invitees
