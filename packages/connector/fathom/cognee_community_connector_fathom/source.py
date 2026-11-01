"""DLT resource for ingesting Fathom meetings into Cognee."""

from __future__ import annotations

from typing import Any

import dlt

from cognee_community_connector_fathom.fathom import FathomClient


def _meeting_record(meeting: dict[str, Any]) -> dict[str, Any]:
    """Normalize a Fathom meeting while keeping invitees as structured data."""
    meeting_id = meeting.get("recording_id", meeting.get("id"))
    if meeting_id is None:
        raise ValueError("Fathom meeting is missing its recording ID.")

    invitees = (meeting.get("calendar_invitees") or meeting.get("invitees") or meeting.get("attendees") or [])
    invitee_emails = [
        item["email"]
        for item in invitees
        if isinstance(item, dict) and item.get("email")
    ]

    summary_data = meeting.get("default_summary") or meeting.get("summary") or ""
    summary = (
        summary_data.get("markdown_formatted") or ""
        if isinstance(summary_data, dict)
        else summary_data
    )

    action_items = meeting.get("action_items") or []
    normalized_actions = []
    for item in action_items:
        if not isinstance(item, dict):
            continue
        assignee = item.get("assignee")
        if isinstance(assignee, dict):
            assignee = assignee.get("name") or assignee.get("email")

        normalized_actions.append(
            {
                "description": item.get("description") or item.get("text") or "",
                "assignee": assignee,
                "completed": item.get("completed", item.get("is_completed")),
            }
        )

    return {
        "meeting_id": str(meeting_id),
        "title": meeting.get("title") or meeting.get("meeting_title") or "",
        "created_at": meeting.get("created_at"),
        "url": meeting.get("url") or meeting.get("share_url"),
        "summary": summary,
        "invitee_emails": invitee_emails,
        "action_items": normalized_actions,
        "transcript": meeting.get("transcript"),
    }


@dlt.resource(
    name="fathom_meetings",
    primary_key="meeting_id",
    write_disposition="replace",
)
def fathom_meetings(
    api_key: str,
    created_after: str | None = None,
    include_transcripts: bool = False,
):
    """Yield normalized meetings from Fathom's paginated API."""
    client = FathomClient(
        api_key,
        include_transcripts=include_transcripts,
    )
    for meeting in client.iter_meetings(created_after=created_after):
        yield _meeting_record(meeting)


def fathom_source(
    api_key: str,
    created_after: str | None = None,
    include_transcripts: bool = False,
):
    """Return the configured Fathom DLT resource."""
    resource = fathom_meetings(
        api_key=api_key,
        created_after=created_after,
        include_transcripts=include_transcripts,
    )
    return resource.apply_hints(
        write_disposition="merge" if created_after else "replace"
    )


