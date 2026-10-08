from typing import Any

from cognee_community_connector_tldv.tldv import (
    DOCUMENT_SOURCE_ATTR,
    TLDVClient,
    _format_meeting_to_row,
    tldv_source,
)


class FakeTLDVClient:
    def __init__(self, meetings_data: list[dict[str, Any]] | None = None) -> None:
        self.meetings_data = meetings_data or [
            {
                "id": "meeting_001",
                "title": "Quarterly Product Planning",
                "organizer": {"name": "Alice Product Lead", "email": "alice@example.com"},
                "happenedAt": "2026-10-01T14:00:00Z",
                "duration": 3600,
                "participants": [
                    {"name": "Alice Product Lead", "email": "alice@example.com"},
                    {"name": "Bob Tech Lead", "email": "bob@example.com"},
                ],
            },
            {
                "id": "meeting_002",
                "title": "Backend Architecture Sync",
                "organizer": {"name": "Bob Tech Lead", "email": "bob@example.com"},
                "happenedAt": "2026-10-02T10:00:00Z",
                "duration": 1800,
                "participants": [
                    {"name": "Bob Tech Lead", "email": "bob@example.com"},
                    {"name": "Charlie SRE", "email": "charlie@example.com"},
                ],
            },
        ]

    def get_meetings(
        self,
        limit: int = 50,
        page: int = 1,
        from_date: str | None = None,
        to_date: str | None = None,
    ) -> dict[str, Any]:
        if page > 1:
            return {"data": [], "pagination": {"page": page, "totalPages": 1}}
        return {
            "data": self.meetings_data,
            "pagination": {"page": 1, "totalPages": 1},
        }

    def get_transcript(self, meeting_id: str) -> list[dict[str, Any]]:
        if meeting_id == "meeting_001":
            return [
                {"speaker": "Alice Product Lead", "text": "Let's review the Q4 milestones."},
                {"speaker": "Bob Tech Lead", "text": "The distributed vector engine is on track."},
            ]
        return [
            {"speaker": "Bob Tech Lead", "text": "We need to scale the partition handler."},
            {"speaker": "Charlie SRE", "text": "I will deploy the new replica pool."},
        ]

    def get_notes(self, meeting_id: str) -> dict[str, Any]:
        if meeting_id == "meeting_001":
            return {
                "summary": "Discussed Q4 roadmap milestones and distributed graph indexing.",
                "actionItems": [
                    "Bob to finalize vector engine benchmarks",
                    "Alice to sync with design team",
                ],
            }
        return {
            "summary": "Discussed scaling partition handler and deploying replica pool.",
            "actionItems": [
                "Charlie to configure replica autoscaling",
            ],
        }


def test_tldv_client_headers() -> None:
    client = TLDVClient(api_key="secret_token_123")
    headers = client._headers()
    assert headers["x-api-key"] == "secret_token_123"
    assert headers["Authorization"] == "Bearer secret_token_123"


def test_tldv_format_meeting_to_row() -> None:
    meeting = {
        "id": "meet_99",
        "title": "Strategy Sync",
        "organizer": {"name": "Elena"},
        "happenedAt": "2026-10-04T12:00:00Z",
        "duration": 1200,
        "participants": [{"name": "Elena"}, {"name": "David"}],
    }
    transcript = [
        {"speaker": "Elena", "text": "We need to launch the community portal."},
        {"speaker": "David", "text": "The API docs are updated."},
    ]
    notes = {
        "summary": "Reviewing community launch timeline.",
        "actionItems": ["Elena to approve release notes"],
    }

    doc = _format_meeting_to_row(meeting, transcript=transcript, notes=notes)
    assert doc is not None
    assert doc["id"] == "tldv_meeting_meet_99"
    assert doc["title"] == "Strategy Sync"
    assert "Meeting: Strategy Sync" in doc["text"]
    assert "- **Organizer:** Elena" in doc["text"]
    assert "Elena, David" in doc["text"]
    assert "Executive Summary" in doc["text"]
    assert "Action Items" in doc["text"]
    assert "- Elena to approve release notes" in doc["text"]
    assert "Elena: We need to launch the community portal." in doc["text"]


def test_tldv_source_iteration() -> None:
    fake_client = FakeTLDVClient()
    source = tldv_source(api_key="test_key", client=fake_client)

    records = list(source)
    assert len(records) == 2
    assert records[0]["id"] == "tldv_meeting_meeting_001"
    assert records[0]["title"] == "Quarterly Product Planning"
    assert "Alice to sync with design team" in records[0]["text"]
    assert "Bob Tech Lead: The distributed vector engine is on track." in records[0]["text"]

    assert records[1]["id"] == "tldv_meeting_meeting_002"
    assert records[1]["title"] == "Backend Architecture Sync"


def test_tldv_document_source_attribute() -> None:
    fake_client = FakeTLDVClient()
    source = tldv_source(api_key="test_key", client=fake_client)
    assert hasattr(source, DOCUMENT_SOURCE_ATTR)
    assert getattr(source, DOCUMENT_SOURCE_ATTR) == "tldv"
    assert DOCUMENT_SOURCE_ATTR == "cognee_document_source"


def test_tldv_source_since_filter() -> None:
    fake_client = FakeTLDVClient()
    source = tldv_source(api_key="test_key", since="2026-10-02T00:00:00Z", client=fake_client)

    records = list(source)
    assert len(records) == 1
    assert records[0]["id"] == "tldv_meeting_meeting_002"


def test_tldv_pagination() -> None:
    fake_client = FakeTLDVClient()
    # Mock multi-page responses
    page1 = {
        "data": [{"id": "m1", "title": "Meeting 1", "happenedAt": "2026-10-01T10:00:00Z"}],
        "pagination": {"page": 1, "totalPages": 2},
    }
    page2 = {
        "data": [{"id": "m2", "title": "Meeting 2", "happenedAt": "2026-10-02T10:00:00Z"}],
        "pagination": {"page": 2, "totalPages": 2},
    }

    def mock_get_meetings(limit=50, page=1, **kwargs):
        return page1 if page == 1 else page2

    fake_client.get_meetings = mock_get_meetings
    source = tldv_source(api_key="test_key", client=fake_client)
    records = list(source)
    assert len(records) == 2
    assert records[0]["id"] == "tldv_meeting_m1"
    assert records[1]["id"] == "tldv_meeting_m2"


def test_tldv_format_meeting_empty_fallback() -> None:
    # Meeting with missing ID should return None
    assert _format_meeting_to_row({}) is None
