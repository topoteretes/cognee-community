"""Unit tests for the Screenpipe connector for cognee."""

import pytest
from unittest.mock import MagicMock

from cognee_community_connector_screenpipe import (
    SCREENPIPE_MEETINGS_TABLE,
    SCREENPIPE_MEMORIES_TABLE,
    SCREENPIPE_SCREEN_TABLE,
    SCREENPIPE_SOURCE_NAME,
    iter_screenpipe_meetings,
    iter_screenpipe_memories,
    iter_screenpipe_screen_text,
    screenpipe_source,
)

try:
    from cognee.tasks.ingestion.dlt_utils import DOCUMENT_SOURCE_ATTR
except ImportError:
    DOCUMENT_SOURCE_ATTR = "cognee_document_source"


MOCK_MEETINGS_DATA = [
    {
        "id": "meet_1",
        "title": "Quarterly Planning",
        "app_name": "Google Meet",
        "meeting_start": "2026-11-02T14:00:00Z",
        "meeting_end": "2026-11-02T15:00:00Z",
        "attendees": ["Alice", "Bob"],
        "user_notes": "Agreed on Q1 goals.",
        "device_id": "laptop-1",
    },
    {
        "id": "meet_in_progress",
        "title": "Active Call",
        "meeting_start": "2026-11-02T16:00:00Z",
        "meeting_end": None,  # Ongoing - should be skipped
    },
    {
        "id": "meet_deleted",
        "_deleted": True,
    },
]

MOCK_TRANSCRIPT_DATA = [
    {
        "speaker": "Alice",
        "timestamp": "14:05:00",
        "text": "Welcome everyone to our planning sync.",
    },
    {
        "speaker": "Bob",
        "timestamp": "14:10:00",
        "text": "I will coordinate the database schema migration.",
    },
]

MOCK_MEMORIES_DATA = [
    {
        "id": "mem_1",
        "content": "Follow up with DevRel team regarding hackathon.",
        "tags": ["devrel", "hackathon"],
        "created_at": "2026-11-02T12:00:00Z",
        "updated_at": "2026-11-02T12:30:00Z",
        "device_id": "laptop-1",
    },
    {
        "id": "mem_deleted",
        "is_deleted": True,
    },
]

MOCK_SEARCH_DATA = [
    {
        "app_name": "VS Code",
        "text": "async def test_feature():\n    assert True",
        "timestamp": "2026-11-02T15:10:00Z",
    },
    {
        "app_name": "VS Code",
        "text": "async def test_feature():\n    assert True",  # Duplicate line to test dedup
        "timestamp": "2026-11-02T15:15:00Z",
    },
    {
        "app_name": "UntrackedApp",
        "text": "Private personal info",
        "timestamp": "2026-11-02T15:20:00Z",
    },
]


def make_mock_client(meetings=None, transcript=None, memories=None, search=None):
    def client(endpoint, params=None):
        if endpoint == "/meetings":
            return meetings if meetings is not None else MOCK_MEETINGS_DATA
        if endpoint.startswith("/meetings/") and endpoint.endswith("/transcript"):
            return transcript if transcript is not None else MOCK_TRANSCRIPT_DATA
        if endpoint == "/memories":
            return memories if memories is not None else MOCK_MEMORIES_DATA
        if endpoint == "/search":
            return search if search is not None else MOCK_SEARCH_DATA
        return []
    return client


def test_iter_meetings_filters_finished_and_formats_transcript():
    client = make_mock_client()
    records = list(iter_screenpipe_meetings(client=client))

    # Expect meet_1 and meet_deleted; meet_in_progress should be skipped
    assert len(records) == 2

    live = next(r for r in records if r["id"] == "meeting:meet_1")
    assert live["meeting_id"] == "meet_1"
    assert live["title"] == "Meeting: Quarterly Planning"
    assert live["app_name"] == "Google Meet"
    assert live["_deleted"] is False
    assert "Alice" in live["attendees"]
    assert "Agreed on Q1 goals" in live["content"]
    assert "[Alice] (14:05:00): Welcome everyone" in live["content"]
    assert "[Bob] (14:10:00): I will coordinate" in live["content"]

    deleted = next(r for r in records if r["id"] == "meeting:meet_deleted")
    assert deleted["_deleted"] is True


def test_iter_memories_yields_structured_data_and_deletions():
    client = make_mock_client()
    records = list(iter_screenpipe_memories(client=client))

    assert len(records) == 2

    live = next(r for r in records if r["id"] == "memory:mem_1")
    assert live["memory_id"] == "mem_1"
    assert "devrel, hackathon" in live["tags"]
    assert "Follow up with DevRel team" in live["content"]
    assert live["_deleted"] is False

    deleted = next(r for r in records if r["id"] == "memory:mem_deleted")
    assert deleted["_deleted"] is True


def test_iter_screen_text_respects_allowlist_and_deduplicates():
    client = make_mock_client()
    # Only allow VS Code; UntrackedApp should be filtered out
    records = list(
        iter_screenpipe_screen_text(
            app_allowlist=["VS Code"],
            device_id="dev-box",
            client=client,
        )
    )

    assert len(records) == 1
    doc = records[0]
    assert doc["id"].startswith("screen:dev-box:VS Code:2026-11-02T15")
    assert doc["app_name"] == "VS Code"
    assert "async def test_feature():" in doc["content"]
    # Check deduplication: the duplicate text should not be duplicated in lines
    assert doc["content"].count("async def test_feature():") == 1
    assert "Private personal info" not in doc["content"]


def test_screenpipe_source_requires_dlt():
    import builtins

    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name == "dlt":
            raise ImportError("no dlt")
        return real_import(name, *args, **kwargs)

    client = make_mock_client()
    try:
        builtins.__import__ = fake_import
        with pytest.raises(ImportError, match=r"cognee\[dlt\]"):
            screenpipe_source(client=client)
    finally:
        builtins.__import__ = real_import


def test_screenpipe_source_declarations():
    pytest.importorskip("dlt")
    client = make_mock_client()
    resources = screenpipe_source(
        include_meetings=True,
        include_memories=True,
        include_screen_text=True,
        app_allowlist=["VS Code"],
        client=client,
    )

    assert len(resources) == 3
    res_names = [r.name for r in resources]
    assert SCREENPIPE_MEETINGS_TABLE in res_names
    assert SCREENPIPE_MEMORIES_TABLE in res_names
    assert SCREENPIPE_SCREEN_TABLE in res_names

    for r in resources:
        assert getattr(r, DOCUMENT_SOURCE_ATTR) == SCREENPIPE_SOURCE_NAME
        assert r.write_disposition == "merge"
        assert r.primary_key == ["id"] or r.primary_key == "id"


def test_resilient_on_server_failure():
    def failing_client(endpoint, **kw):
        raise ConnectionError("Connection refused to localhost:3030")

    # Functions should catch gracefully and yield nothing rather than crashing
    assert list(iter_screenpipe_meetings(client=failing_client)) == []
    assert list(iter_screenpipe_memories(client=failing_client)) == []
    assert list(iter_screenpipe_screen_text(app_allowlist=["App"], client=failing_client)) == []

