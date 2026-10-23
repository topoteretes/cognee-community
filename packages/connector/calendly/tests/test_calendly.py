"""Tests for the Calendly connector."""

import pytest
from unittest.mock import patch, MagicMock


def _make_event(uri="https://api.calendly.com/scheduled_events/ABC123", name="Test Meeting"):
    return {
        "uri": uri,
        "name": name,
        "start_time": "2026-09-20T10:00:00Z",
        "end_time": "2026-09-20T11:00:00Z",
        "status": "active",
        "location": {"type": "zoom", "join_url": "https://zoom.us/j/123"},
        "event_type": "https://api.calendly_event_types/DEF456",
        "cancelled": False,
        "created_at": "2026-09-18T10:00:00Z",
        "updated_at": "2026-09-18T10:00:00Z",
    }


def _make_invitee(name="Test User", email="test@example.com"):
    return {
        "name": name,
        "email": email,
        "questions_and_answers": [],
        "timezone": "Asia/Kolkata",
    }


def test_calendly_source_no_token():
    """Should raise ValueError when no token is provided."""
    from cognee_community_connector_calendly.calendly import calendly_source

    with patch.dict("os.environ", {}, clear=True):
        with pytest.raises(ValueError, match="CALENDLY_API_KEY"):
            calendly_source()


def test_calendly_source_with_token():
    """Should create a dlt source when token is provided."""
    from cognee_community_connector_calendly.calendly import calendly_source

    with patch.dict("os.environ", {"CALENDLY_API_KEY": "test_token"}):
        with patch(
            "cognee_community_connector_calendly.calendly._calendly_get"
        ) as mock_get:
            mock_get.side_effect = [
                {"resource": {"uri": "https://api.calendly.com/users/USER123"}},
                {"collection": [_make_event()]},
                {"collection": []},
            ]
            source = calendly_source()
            assert source is not None


def test_calendly_source_yields_documents():
    """Should yield structured documents from Calendly events."""
    from cognee_community_connector_calendly.calendly import calendly_source

    with patch.dict("os.environ", {"CALENDLY_API_KEY": "test_token"}):
        with patch(
            "cognee_community_connector_calendly.calendly._calendly_get"
        ) as mock_get:
            mock_get.side_effect = [
                {"resource": {"uri": "https://api.calendly.com/users/USER123"}},
                {"collection": [_make_event()]},
                {"collection": [_make_invitee()]},
            ]
            source = calendly_source()
            docs = list(source())
            assert len(docs) == 1
            assert docs[0]["name"] == "Test Meeting"
            assert docs[0]["status"] == "active"
            assert len(docs[0]["attendees"]) == 1


def test_calendly_source_handles_canceled_events():
    """Should include canceled events when status filter allows."""
    from cognee_community_connector_calendly.calendly import calendly_source

    canceled_event = _make_event(uri="https://api.calendly.com/scheduled_events/CANCEL123")
    canceled_event["cancelled"] = True
    canceled_event["status"] = "canceled"

    with patch.dict("os.environ", {"CALENDLY_API_KEY": "test_token"}):
        with patch(
            "cognee_community_connector_calendly.calendly._calendly_get"
        ) as mock_get:
            mock_get.side_effect = [
                {"resource": {"uri": "https://api.calendly.com/users/USER123"}},
                {"collection": [canceled_event]},
                {"collection": []},
            ]
            source = calendly_source(status="canceled")
            docs = list(source())
            assert len(docs) == 1
            assert docs[0]["cancelled"] is True


def test_calendly_source_empty_events():
    """Should handle empty event list."""
    from cognee_community_connector_calendly.calendly import calendly_source

    with patch.dict("os.environ", {"CALENDLY_API_KEY": "test_token"}):
        with patch(
            "cognee_community_connector_calendly.calendly._calendly_get"
        ) as mock_get:
            mock_get.side_effect = [
                {"resource": {"uri": "https://api.calendly.com/users/USER123"}},
                {"collection": []},
            ]
            source = calendly_source()
            docs = list(source())
            assert len(docs) == 0


def test_calendly_source_invitee_fetch_failure():
    """Should handle invitee fetch failure gracefully."""
    from cognee_community_connector_calendly.calendly import calendly_source

    with patch.dict("os.environ", {"CALENDLY_API_KEY": "test_token"}):
        with patch(
            "cognee_community_connector_calendly.calendly._calendly_get"
        ) as mock_get:
            mock_get.side_effect = [
                {"resource": {"uri": "https://api.calendly.com/users/USER123"}},
                {"collection": [_make_event()]},
                Exception("Rate limited"),
            ]
            source = calendly_source()
            docs = list(source())
            assert len(docs) == 1
            assert docs[0]["attendees"] == []


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
