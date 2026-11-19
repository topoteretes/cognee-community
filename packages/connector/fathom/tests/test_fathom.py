import requests
import responses

from cognee_community_connector_fathom.fathom import API_BASE_URL, FathomClient
from cognee_community_connector_fathom.source import fathom_source


@responses.activate
def test_meetings_use_api_key_and_follow_next_cursor():
    url = f"{API_BASE_URL}/meetings"
    responses.add(
        responses.GET,
        url,
        json={
            "items": [{"recording_id": 123, "title": "First meeting"}],
            "next_cursor": "page-2",
        },
        status=200,
    )
    responses.add(
        responses.GET,
        url,
        json={
            "items": [{"recording_id": 456, "title": "Second meeting"}],
            "next_cursor": None,
        },
        status=200,
    )

    client = FathomClient("test-api-key")
    meetings = list(client.iter_meetings())

    assert [meeting["recording_id"] for meeting in meetings] == [123, 456]
    assert all(
        call.request.headers["X-Api-Key"] == "test-api-key"
        for call in responses.calls
    )
    assert len(responses.calls) == 2
    assert responses.calls[1].request.params == {"cursor": "page-2"}


def test_empty_api_key_is_rejected():
    try:
        FathomClient("")
    except ValueError:
        return
    raise AssertionError("An empty API key should be rejected.")


@responses.activate
def test_created_after_and_transcript_parameter():
    responses.add(
        responses.GET,
        f"{API_BASE_URL}/meetings",
        json={"items": [], "next_cursor": None},
        status=200,
    )

    client = FathomClient("test-api-key", include_transcripts=True)
    list(client.iter_meetings(created_after="2026-10-01T00:00:00Z"))

    params = responses.calls[0].request.params
    assert params["created_after"] == "2026-10-01T00:00:00Z"
    assert params["include_transcript"] == "true"


from cognee_community_connector_fathom.source import _meeting_record


def test_meeting_record_normalizes_meeting_details():
    record = _meeting_record({
        "recording_id": 123,
        "title": "Project Review",
        "created_at": "2026-10-01T10:00:00Z",
        "url": "https://example.com/meeting",
        "default_summary": "Discussed project progress",
        "invitees": [{"email": "person@example.com"}, {"name": "No email"}],
        "action_items": [
            {"description": "Send report", "assignee": "Alex", "completed": True}
        ],
    })

    assert record["meeting_id"] == "123"
    assert record["title"] == "Project Review"
    assert record["summary"] == "Discussed project progress"
    assert record["invitee_emails"] == ["person@example.com"]
    assert record["action_items"] == [
        {"description": "Send report", "assignee": "Alex", "completed": True}
    ]


def test_meeting_record_rejects_missing_id():
    import pytest

    with pytest.raises(ValueError, match="recording ID"):
        _meeting_record({"title": "Meeting without ID"})


def test_full_sync_uses_replace():
    resource = fathom_source("test-key")
    assert resource._hints.get("write_disposition") == "replace"


def test_incremental_sync_uses_merge():
    resource = fathom_source(
        "test-key",
        created_after="2026-10-01T00:00:00Z",
    )
    assert resource._hints.get("write_disposition") == "merge"



def test_normalizes_fathom_api_response_shapes():
    record = _meeting_record({
        "recording_id": 789,
        "calendar_invitees": [{"name": "Sam", "email": "sam@example.com"}],
        "default_summary": {"markdown_formatted": "Discussed the launch"},
        "action_items": [
            {
                "description": "Publish notes",
                "assignee": {"name": "Sam", "email": "sam@example.com"},
                "completed": False,
            }
        ],
    })

    assert record["invitee_emails"] == ["sam@example.com"]
    assert record["summary"] == "Discussed the launch"
    assert record["action_items"][0]["assignee"] == "Sam"
    assert record["action_items"][0]["completed"] is False


def test_transcript_preserves_speaker_attribution():
    record = _meeting_record({
        "recording_id": 790,
        "transcript": [
            {
                "speaker": {
                    "display_name": "Sam",
                    "matched_calendar_invitee_email": "sam@example.com",
                },
                "text": "We should ship on Friday.",
                "timestamp": "00:05:32",
            }
        ],
    })

    assert record["transcript"][0]["speaker"]["display_name"] == "Sam"
    assert record["transcript"][0]["text"] == "We should ship on Friday."
