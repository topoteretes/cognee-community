"""Unit tests for the Fireflies.ai data-source connector.

All tests are 100% offline and execute against mocked responses without requiring
live API keys or network traffic, ensuring reliable execution in CI pipelines.
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from cognee_community_connector_fireflies.fireflies import (
    DOCUMENT_SOURCE_ATTR,
    FIREFLIES_SOURCE_NAME,
    FIREFLIES_TABLE_NAME,
    _deleted_row,
    _execute_graphql,
    _format_timestamp,
    _is_transient,
    _render_transcript_content,
    _resolve_speaker,
    _retry_after,
    _transcript_to_row,
    fireflies_source,
    sync_transcripts,
)

# ---------------------------------------------------------------------------
# Test Fixtures & Mock Helpers
# ---------------------------------------------------------------------------


def _sample_attendees() -> list[dict[str, Any]]:
    return [
        {"displayName": "Alice Smith", "email": "alice@example.com"},
        {"displayName": "Bob Jones", "email": "bob@example.com"},
    ]


def _sample_transcript(
    t_id: str = "t_101",
    title: str = "Sprint Planning Sync",
    date: float = 1713283200000.0,
) -> dict[str, Any]:
    return {
        "id": t_id,
        "title": title,
        "date": date,
        "duration": 45,
        "organizer_email": "alice@example.com",
        "participants": ["alice@example.com", "bob@example.com"],
        "meeting_attendees": _sample_attendees(),
        "summary": {
            "overview": "Team discussed the v2 API migration roadmap.",
            "shorthand_bullet": ["Agreed to deprecate v1 endpoints", "Bob to set up staging"],
            "keywords": ["API", "v2 migration", "staging"],
            "action_items": [
                "Alice: Update rate-limit docs",
                "Bob: Deploy staging environment",
            ],
        },
        "sentences": [
            {
                "index": 0,
                "speaker_name": "Alice Smith",
                "speaker_id": "0",
                "text": "Welcome everyone to sprint planning.",
                "start_time": 10.5,
                "end_time": 15.0,
            },
            {
                "index": 1,
                "speaker_name": "Bob Jones",
                "speaker_id": "1",
                "text": "Staging environment will be ready by Wednesday.",
                "start_time": 75.0,
                "end_time": 82.0,
            },
        ],
    }


class FakeGraphQLResponse:
    """Mock HTTP response for requests session."""

    def __init__(self, json_data: dict[str, Any], status_code: int = 200, headers: dict | None = None):
        self._json_data = json_data
        self.status_code = status_code
        self.headers = headers or {}

    def json(self) -> dict[str, Any]:
        return self._json_data

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            import requests

            raise requests.HTTPError(f"HTTP {self.status_code}")


class FakeSession:
    """Mock requests session for GraphQL requests."""

    def __init__(self, responses: list[FakeGraphQLResponse] | None = None):
        self._responses = list(responses or [])
        self.headers = {}
        self.post_calls: list[dict[str, Any]] = []

    def post(self, url: str, json: dict[str, Any] | None = None, **kwargs) -> FakeGraphQLResponse:
        self.post_calls.append({"url": url, "json": json, "kwargs": kwargs})
        if self._responses:
            return self._responses.pop(0)
        return FakeGraphQLResponse({"data": {"transcripts": []}})


# ---------------------------------------------------------------------------
# Speaker Attribution & Formatting Tests
# ---------------------------------------------------------------------------


def test_resolve_speaker_exact_match():
    attendees = _sample_attendees()
    speaker = _resolve_speaker("Alice Smith", "0", attendees)
    assert speaker == "Alice Smith (alice@example.com)"


def test_resolve_speaker_partial_or_email_match():
    attendees = [{"displayName": "", "email": "carol@example.com"}]
    speaker = _resolve_speaker("carol", "2", attendees)
    assert "carol@example.com" in speaker


def test_resolve_speaker_unresolved_fallback():
    attendees = _sample_attendees()
    speaker = _resolve_speaker("External Guest", "9", attendees)
    assert speaker == "External Guest"

    speaker_unnamed = _resolve_speaker(None, "4", attendees)
    assert speaker_unnamed == "Speaker 4"


def test_format_timestamp():
    assert _format_timestamp(65) == "[01:05]"
    assert _format_timestamp(3665) == "[01:01:05]"
    assert _format_timestamp(None) == ""
    assert _format_timestamp("invalid") == ""


def test_render_transcript_content():
    transcript = _sample_transcript()
    markdown = _render_transcript_content(transcript)

    assert "# Meeting: Sprint Planning Sync" in markdown
    assert "Alice Smith (alice@example.com)" in markdown
    assert "Bob Jones (bob@example.com)" in markdown
    assert "## Executive Summary" in markdown
    assert "## Action Items" in markdown
    assert "## Discussion & Speaker Transcript" in markdown
    assert "[01:15] **Bob Jones (bob@example.com):** Staging environment will be ready by Wednesday." in markdown


def test_transcript_to_row():
    transcript = _sample_transcript("t_999", "Architecture Review")
    row = _transcript_to_row(transcript)

    assert row["id"] == "t_999"
    assert row["title"] == "Architecture Review"
    assert row["url"] == "https://app.fireflies.ai/view/t_999"
    assert "Architecture Review" in row["content"]
    assert row["date"] == 1713283200000.0


def test_deleted_row():
    row = _deleted_row("t_123")
    assert row == {"id": "t_123", "_deleted": True}


# ---------------------------------------------------------------------------
# Retry & Rate-Limit Logic Tests
# ---------------------------------------------------------------------------


def test_is_transient():
    assert _is_transient(429) is True
    assert _is_transient(500) is True
    assert _is_transient(503) is True
    assert _is_transient(200) is False
    assert _is_transient(404) is False


def test_retry_after_header():
    assert _retry_after({"retry-after": "5"}, 0) == 5.0
    assert _retry_after({"Retry-After": "10"}, 0) == 10.0
    assert _retry_after({}, 2) == 4.0  # 2^2


def test_execute_graphql_success():
    resp = FakeGraphQLResponse({"data": {"transcripts": [{"id": "1"}]}})
    session = FakeSession([resp])
    data = _execute_graphql(session, "https://api.test/graphql", "query { test }")
    assert "transcripts" in data
    assert data["transcripts"][0]["id"] == "1"


# ---------------------------------------------------------------------------
# Sync & Incremental Cursor Tests
# ---------------------------------------------------------------------------


def test_sync_transcripts_initial_full_load():
    transcripts_data = [
        _sample_transcript("m_1", "Sprint 1", 1000.0),
        _sample_transcript("m_2", "Sprint 2", 2000.0),
    ]
    resp1 = FakeGraphQLResponse({"data": {"transcripts": transcripts_data}})
    resp2 = FakeGraphQLResponse({"data": {"transcripts": []}})
    session = FakeSession([resp1, resp2])

    state: dict[str, Any] = {}
    rows = list(sync_transcripts(session, state, limit=2))

    assert len(rows) == 2
    assert rows[0]["id"] == "m_1"
    assert rows[1]["id"] == "m_2"
    assert state["known_ids"] == ["m_1", "m_2"]
    assert state["last_synced_date"] == 2000.0


def test_sync_transcripts_incremental_filter():
    # Only returns meeting m_3 newer than cursor
    m3 = _sample_transcript("m_3", "Sprint 3", 3000.0)
    resp = FakeGraphQLResponse({"data": {"transcripts": [m3]}})
    session = FakeSession([resp])

    state = {"known_ids": ["m_1", "m_2"], "last_synced_date": 2000.0}
    rows = list(sync_transcripts(session, state, limit=50))

    assert len(rows) == 1
    assert rows[0]["id"] == "m_3"
    assert state["known_ids"] == ["m_1", "m_2", "m_3"]
    assert state["last_synced_date"] == 3000.0


def test_sync_transcripts_forget_on_delete_tombstones():
    # Previously had m_1, m_2. Now only m_2 returned in full snapshot
    m2 = _sample_transcript("m_2", "Sprint 2", 2000.0)
    resp = FakeGraphQLResponse({"data": {"transcripts": [m2]}})
    session = FakeSession([resp])

    state = {"known_ids": ["m_1", "m_2"], "last_synced_date": 0.0}
    rows = list(sync_transcripts(session, state, limit=50))

    # Expect m_2 updated row and m_1 tombstone
    assert len(rows) == 2
    assert rows[0]["id"] == "m_2"
    assert rows[1] == {"id": "m_1", "_deleted": True}
    assert state["known_ids"] == ["m_2"]


def test_sync_transcripts_empty_sweep_guard():
    # If API returns 0 records when meetings were known, do NOT purge
    resp = FakeGraphQLResponse({"data": {"transcripts": []}})
    session = FakeSession([resp])

    state = {"known_ids": ["m_1", "m_2"], "last_synced_date": 0.0}
    rows = list(sync_transcripts(session, state, limit=50))

    assert len(rows) == 0
    # known_ids preserved!
    assert state["known_ids"] == ["m_1", "m_2"]


# ---------------------------------------------------------------------------
# Source Factory & Document Mode Tagging
# ---------------------------------------------------------------------------


def test_fireflies_source_configuration():
    mock_dlt = MagicMock()
    mock_resource = MagicMock()
    mock_dlt.resource.return_value = lambda fn: mock_resource
    mock_source_obj = MagicMock()
    mock_source_obj.name = FIREFLIES_SOURCE_NAME
    mock_dlt.source.return_value = lambda fn: (lambda: mock_source_obj)

    with patch.dict("sys.modules", {"dlt": mock_dlt}):
        session = FakeSession()
        source = fireflies_source(api_key="test_key", session=session)

        # Must declare document source attribute for cognify routing
        assert getattr(source, DOCUMENT_SOURCE_ATTR) == FIREFLIES_SOURCE_NAME
        assert source.name == FIREFLIES_SOURCE_NAME


def test_fireflies_source_missing_auth():
    with patch.dict("os.environ", {}, clear=True):
        with pytest.raises(ValueError, match="Fireflies API key is required"):
            fireflies_source(api_key=None, session=None)
