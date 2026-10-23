"""Unit and pipeline integration tests for the Calendly dlt connector.

Two layers, all runnable in CI without a live Calendly token:
* Layer A: DB-free unit tests for markdown formatting, invitee Q&A parsing,
  content hashing, pagination termination guards, and document-mode marker.
* Layer B: dlt pipeline tests (mocked requests.Session, temp sqlite destination)
  covering initial sync, edit updates, canceled/deleted event removal (forget-on-delete),
  and time window filtering.
"""

from typing import Any
from urllib.parse import parse_qs, urlparse

import pytest
import requests
from cognee.tasks.ingestion.dlt_utils import document_source_tag

from cognee_community_connector_calendly.calendly import (
    CALENDLY_SOURCE_NAME,
    CALENDLY_TABLE_NAME,
    _build_event_content,
    _event_to_row,
    _extract_uuid,
    _format_location,
    _paginate,
    calendly_source,
)

# ---------------------------------------------------------------------------
# Test Fixtures & Fakes
# ---------------------------------------------------------------------------


def _mock_event(
    event_uuid: str,
    name: str = "Intro Call",
    status: str = "active",
    start_time: str = "2026-10-15T10:00:00Z",
    end_time: str = "2026-10-15T10:30:00Z",
    description: str | None = "Discussion on project scope",
    location: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "uri": f"https://api.calendly.com/scheduled_events/{event_uuid}",
        "name": name,
        "status": status,
        "start_time": start_time,
        "end_time": end_time,
        "created_at": "2026-10-01T09:00:00Z",
        "description": description,
        "location": location or {"type": "zoom", "join_url": "https://zoom.us/j/123456789"},
        "event_memberships": [{"user_name": "Host Alice", "user_email": "alice@example.com"}],
    }


def _mock_invitee(
    name: str = "Bob Client",
    email: str = "bob@example.com",
    questions_and_answers: list[dict[str, Any]] | None = None,
    status: str = "active",
) -> dict[str, Any]:
    return {
        "name": name,
        "email": email,
        "status": status,
        "timezone": "America/New_York",
        "questions_and_answers": questions_and_answers
        or [
            {"question": "What is your main goal?", "answer": "Integrate Cognee memory into CRM."},
            {"question": "Team size?", "answer": "10 developers."},
        ],
    }


class FakeCalendlySession(requests.Session):
    """Mock session intercepting requests to Calendly endpoints."""

    def __init__(
        self,
        events: list[dict[str, Any]],
        invitees_map: dict[str, list[dict[str, Any]]] | None = None,
    ):
        super().__init__()
        self._events = events
        self._invitees_map = invitees_map or {}
        self.request_history: list[dict[str, Any]] = []

    def request(self, method: str, url: str, **kwargs) -> requests.Response:
        self.request_history.append({"method": method, "url": url, "kwargs": kwargs})
        resp = requests.Response()
        resp.status_code = 200

        parsed = urlparse(url)
        path = parsed.path

        if path == "/users/me":
            resp._content = (
                b'{"resource": {"uri": "https://api.calendly.com/users/USER_123", '
                b'"current_organization": "https://api.calendly.com/organizations/ORG_123"}}'
            )
            return resp

        if path == "/scheduled_events":
            params = kwargs.get("params") or {}
            if not params and parsed.query:
                params = {k: v[0] for k, v in parse_qs(parsed.query).items()}

            filtered = list(self._events)
            min_start = params.get("min_start_time")
            if min_start:
                filtered = [e for e in filtered if e.get("start_time", "") >= min_start]

            status = params.get("status")
            if status:
                filtered = [e for e in filtered if e.get("status") == status]

            import json

            data = {
                "collection": filtered,
                "pagination": {"count": len(filtered), "next_page": None},
            }
            resp._content = json.dumps(data).encode("utf-8")
            return resp

        if "/scheduled_events/" in path and path.endswith("/invitees"):
            parts = path.strip("/").split("/")
            event_uuid = parts[-2]
            invitees = self._invitees_map.get(event_uuid, [_mock_invitee()])
            import json

            data = {
                "collection": invitees,
                "pagination": {"count": len(invitees), "next_page": None},
            }
            resp._content = json.dumps(data).encode("utf-8")
            return resp

        resp._content = b'{"collection": [], "pagination": {"count": 0, "next_page": None}}'
        return resp


# ---------------------------------------------------------------------------
# Layer A: Unit Tests (DB-Free)
# ---------------------------------------------------------------------------


def test_extract_uuid():
    assert _extract_uuid("https://api.calendly.com/scheduled_events/ABC-123") == "ABC-123"
    assert _extract_uuid("ABC-123") == "ABC-123"
    assert _extract_uuid("") == ""
    assert _extract_uuid(None) == ""


def test_format_location():
    loc = {"type": "zoom", "join_url": "https://zoom.us/j/123"}
    assert _format_location(loc) == "Zoom (https://zoom.us/j/123)"
    assert _format_location({"type": "in_person"}) == "In_person"
    assert _format_location(None) == ""


def test_build_event_content_includes_invitee_questions_and_notes():
    event = _mock_event("EVT-1", name="Product Demo", description="Review architecture roadmap")
    invitee = _mock_invitee(
        name="Charlie",
        email="charlie@corp.com",
        questions_and_answers=[
            {"question": "What problem are you solving?", "answer": "Knowledge graph ingestion."},
        ],
    )

    content = _build_event_content(event, [invitee])

    assert "# Product Demo" in content
    assert "Host Alice" in content
    assert "Zoom (https://zoom.us/j/123456789)" in content
    assert "Review architecture roadmap" in content
    assert "Charlie (charlie@corp.com)" in content
    assert "What problem are you solving?" in content
    assert "Knowledge graph ingestion." in content


def test_event_to_row_generates_stable_id():
    event = _mock_event("EVT-1")
    invitee = _mock_invitee()

    row1 = _event_to_row(event, [invitee])
    row2 = _event_to_row(event, [invitee])

    assert row1["id"] == row2["id"]
    assert row1["title"] == "Intro Call"
    assert row1["url"] == "https://api.calendly.com/scheduled_events/EVT-1"
    assert len(row1["id"]) == 64  # SHA256 hex length


def test_calendly_source_declares_document_marker():
    session = FakeCalendlySession(events=[])
    source = calendly_source(token="fake-token", session=session)
    assert CALENDLY_SOURCE_NAME == "calendly"
    assert CALENDLY_TABLE_NAME == "calendly_events"
    assert document_source_tag(source) == "calendly"


def test_paginate_follows_next_page():
    session = requests.Session()

    def fake_request(method, url, **kwargs):
        resp = requests.Response()
        resp.status_code = 200
        if "page=2" in url:
            resp._content = b'{"collection": [{"id": 2}], "pagination": {"next_page": null}}'
        else:
            resp._content = b'{"collection": [{"id": 1}], "pagination": {"next_page": "https://api.calendly.com/items?page=2"}}'
        return resp

    session.request = fake_request

    items = list(_paginate(session, "https://api.calendly.com/items", headers={}))
    assert len(items) == 2
    assert items[0]["id"] == 1
    assert items[1]["id"] == 2


# ---------------------------------------------------------------------------
# Layer B: Pipeline Tests (dlt SQLite destination)
# ---------------------------------------------------------------------------


def _run_sync(dlt, tmp_path, events, invitees_map=None):
    """Run calendly_source through a dlt pipeline into a temp sqlite database."""
    session = FakeCalendlySession(events=events, invitees_map=invitees_map)
    db_path = (tmp_path / "calendly.db").as_posix()
    pipeline = dlt.pipeline(
        pipeline_name="calendly_test",
        destination=dlt.destinations.sqlalchemy(f"sqlite:///{db_path}"),
        dataset_name="calendly_ds",
        pipelines_dir=str(tmp_path / "state"),
    )
    pipeline.run(calendly_source(token="test_token", session=session))
    return pipeline


def _read_events(pipeline):
    """Return {id: row-dict} for the calendly_events table."""
    with (
        pipeline.sql_client() as client,
        client.execute_query("SELECT id, title, content FROM calendly_events") as cursor,
    ):
        rows = cursor.fetchall()
    return {row[0]: {"id": row[0], "title": row[1], "content": row[2]} for row in rows}


@pytest.fixture
def dlt_mod():
    return pytest.importorskip("dlt")


def test_first_sync_loads_events_with_qa(dlt_mod, tmp_path):
    events = [_mock_event("EVT-1", name="Kickoff Meeting")]
    invitees = {
        "EVT-1": [
            _mock_invitee(
                name="Dave",
                questions_and_answers=[{"question": "Topics?", "answer": "Search indexing"}],
            )
        ]
    }

    pipeline = _run_sync(dlt_mod, tmp_path, events, invitees)
    rows = _read_events(pipeline)

    assert len(rows) == 1
    row = next(iter(rows.values()))
    assert row["title"] == "Kickoff Meeting"
    assert "Search indexing" in row["content"]


def test_edit_event_is_reflected_on_resync(dlt_mod, tmp_path):
    events_v1 = [_mock_event("EVT-1", description="Version 1 notes")]
    _run_sync(dlt_mod, tmp_path, events_v1)

    events_v2 = [_mock_event("EVT-1", description="Version 2 updated notes")]
    pipeline = _run_sync(dlt_mod, tmp_path, events_v2)

    rows = _read_events(pipeline)
    assert len(rows) == 1
    row = next(iter(rows.values()))
    assert "Version 2 updated notes" in row["content"]
    assert "Version 1 notes" not in row["content"]


def test_canceled_or_deleted_event_is_removed_on_resync(dlt_mod, tmp_path):
    # Two active events initially
    events_initial = [
        _mock_event("EVT-1", name="Event One"),
        _mock_event("EVT-2", name="Event Two"),
    ]
    _run_sync(dlt_mod, tmp_path, events_initial)

    # Re-sync: EVT-1 is canceled, EVT-2 is still active
    events_resync = [
        _mock_event("EVT-1", name="Event One", status="canceled"),
        _mock_event("EVT-2", name="Event Two", status="active"),
    ]
    pipeline = _run_sync(dlt_mod, tmp_path, events_resync)

    rows = _read_events(pipeline)
    # EVT-1 is canceled, so it drops out of the replace snapshot.
    # Staging only retains Event Two, enabling cognee orphan_cleanup to purge EVT-1 downstream.
    assert len(rows) == 1
    row = next(iter(rows.values()))
    assert row["title"] == "Event Two"


def test_min_start_time_filtering(dlt_mod, tmp_path):
    events = [
        _mock_event("EVT-OLD", start_time="2026-09-01T00:00:00Z", name="Old Event"),
        _mock_event("EVT-NEW", start_time="2026-10-15T00:00:00Z", name="New Event"),
    ]
    session = FakeCalendlySession(events=events)
    db_path = (tmp_path / "calendly_filter.db").as_posix()
    pipeline = dlt_mod.pipeline(
        pipeline_name="calendly_filter_test",
        destination=dlt_mod.destinations.sqlalchemy(f"sqlite:///{db_path}"),
        dataset_name="calendly_ds",
        pipelines_dir=str(tmp_path / "state"),
    )
    # Sync with min_start_time filtering
    pipeline.run(
        calendly_source(
            token="test_token",
            min_start_time="2026-10-01T00:00:00Z",
            session=session,
        )
    )

    rows = _read_events(pipeline)
    assert len(rows) == 1
    row = next(iter(rows.values()))
    assert row["title"] == "New Event"
