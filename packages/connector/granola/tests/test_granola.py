"""Unit and pipeline integration tests for the Granola dlt connector.

Two layers, all runnable in CI without a live Granola API key:

* Layer A: DB-free unit tests for markdown formatting, speaker attribution,
  content-hash id stability, pagination termination guards, and document-mode marker.
* Layer B: dlt pipeline tests (mocked requests.Session, temp sqlite destination)
  covering first sync, edit updates, vanished note deletion, deleted_at filtering,
  and failure rollback safety under write_disposition="replace".
"""

import json
from types import SimpleNamespace
from typing import Any

import pytest
import requests
from cognee.tasks.ingestion.dlt_utils import document_source_tag

from cognee_community_connector_granola.granola import (
    GRANOLA_SOURCE_NAME,
    GRANOLA_TABLE_NAME,
    GranolaCursorPaginator,
    _build_note_content,
    _format_speaker,
    _note_to_row,
    _render_transcript_item,
    granola_source,
)

# ---------------------------------------------------------------------------
# Layer A: DB-Free Unit Tests
# ---------------------------------------------------------------------------


def test_format_speaker_resolves_correct_attribution():
    # Both name and relative attribution
    assert _format_speaker({"name": "Alice Smith", "attribution": "me"}) == "me (Alice Smith)"
    # Name only
    assert _format_speaker({"name": "Bob", "attribution": None}) == "Bob"
    # Attribution only
    assert _format_speaker({"name": None, "attribution": "them"}) == "them"
    # Diarization label (e.g. iOS anonymous label)
    assert _format_speaker({"diarization_label": "Speaker A"}) == "Speaker A"
    # Empty / none
    assert _format_speaker(None) == "Unknown"
    assert _format_speaker({}) == "Unknown"


def test_render_transcript_item():
    item_with_time = {
        "speaker": {"name": "Oat Benson", "attribution": "me"},
        "start_time": "2026-01-27T15:30:00Z",
        "text": "Let's review the roadmap.",
    }
    assert (
        _render_transcript_item(item_with_time)
        == "[2026-01-27T15:30:00Z] me (Oat Benson): Let's review the roadmap."
    )

    item_without_time = {
        "speaker": {"attribution": "them"},
        "text": "Sounds good.",
    }
    assert _render_transcript_item(item_without_time) == "them: Sounds good."


def test_build_note_content_full():
    note = {
        "id": "not_123",
        "title": "Q3 GTM sync",
        "created_at": "2026-01-27T15:00:00Z",
        "owner": {"name": "Oat", "email": "oat@granola.ai"},
        "calendar_event": {
            "scheduled_start_time": "2026-01-27T15:30:00Z",
            "organiser": "oat@granola.ai",
        },
        "attendees": [
            {"name": "Oat Benson", "email": "oat@granola.ai"},
            {"name": "Raisin Patel", "email": "raisin@granola.ai"},
        ],
        "summary_markdown": "## Key Takeaways\n- Narrowed target to mid-market finance.",
        "private_notes_markdown": "- Ask about budget limits.",
        "transcript": [
            {
                "speaker": {"name": "Oat Benson", "attribution": "me"},
                "start_time": "2026-01-27T15:30:03Z",
                "text": "Welcome everyone.",
            }
        ],
    }

    content = _build_note_content(note, include_transcript=True)
    assert "# Q3 GTM sync" in content
    assert "- Date: 2026-01-27T15:30:00Z" in content
    assert "- Owner: Oat (oat@granola.ai)" in content
    assert "- Organiser: oat@granola.ai" in content
    assert "- Attendees: Oat Benson, Raisin Patel" in content
    assert "- Note ID: not_123" in content
    assert "## Summary" in content
    assert "Narrowed target to mid-market finance." in content
    assert "## Private Notes" in content
    assert "Ask about budget limits." in content
    assert "## Transcript" in content
    assert "[2026-01-27T15:30:03Z] me (Oat Benson): Welcome everyone." in content


def test_build_note_content_transcript_disabled():
    note = {
        "id": "not_123",
        "title": "Design Sync",
        "summary_markdown": "Agreed on UI layout.",
        "transcript": [
            {
                "speaker": {"name": "Alice"},
                "text": "I like the minimalist direction.",
            }
        ],
    }

    content = _build_note_content(note, include_transcript=False)
    assert "# Design Sync" in content
    assert "Agreed on UI layout." in content
    assert "## Transcript" not in content
    assert "minimalist direction" not in content


def test_id_hash_stability():
    note1 = {
        "id": "not_123",
        "title": "Weekly 1:1",
        "summary_markdown": "Discussed career development.",
    }
    row1 = _note_to_row(note1)
    row2 = _note_to_row(note1)
    assert row1["id"] == row2["id"]

    # When content edits, the sha256 id changes
    note_edited = dict(note1, summary_markdown="Discussed promotions and career development.")
    row_edited = _note_to_row(note_edited)
    assert row_edited["id"] != row1["id"]


def test_paginator_follows_cursor_and_guards_null():
    paginator = GranolaCursorPaginator(
        cursor_path="cursor",
        cursor_param="cursor",
        has_more_path="hasMore",
    )

    # First page: hasMore=True, cursor="cursor_abc"
    resp1 = SimpleNamespace(
        json=lambda: {"notes": [{"id": "n1"}], "hasMore": True, "cursor": "cursor_abc"}
    )
    paginator.update_state(resp1, [{"id": "n1"}])
    assert paginator.has_next_page is True

    req = SimpleNamespace(params={}, json=None)
    paginator.update_request(req)
    assert req.params["cursor"] == "cursor_abc"

    # Second page: normal termination with hasMore=False, cursor=None
    resp2 = SimpleNamespace(
        json=lambda: {"notes": [{"id": "n2"}], "hasMore": False, "cursor": None}
    )
    paginator.update_state(resp2, [{"id": "n2"}])
    assert paginator.has_next_page is False

    # Contract violation guard: hasMore=True but cursor=None -> must terminate
    resp3 = SimpleNamespace(json=lambda: {"notes": [{"id": "n3"}], "hasMore": True, "cursor": None})
    paginator.update_state(resp3, [{"id": "n3"}])
    assert paginator.has_next_page is False


def test_granola_source_requires_api_key(monkeypatch):
    monkeypatch.delenv("GRANOLA_API_KEY", raising=False)
    with pytest.raises(ValueError, match="Granola integration token required"):
        granola_source()


def test_granola_source_declares_document_marker():
    source = granola_source(api_key="grn_test_secret")
    assert GRANOLA_SOURCE_NAME == "granola"
    assert document_source_tag(source) == "granola"
    assert GRANOLA_TABLE_NAME in source.selected_resources


# ---------------------------------------------------------------------------
# Layer B: dlt Pipeline Tests (Mocked Transport + SQLite)
# ---------------------------------------------------------------------------


class MockGranolaTransport(requests.adapters.BaseAdapter):
    """Stand-in HTTP adapter backed by test fixtures."""

    def __init__(
        self,
        notes_list: list[dict],
        note_details: dict[str, dict],
        fail_id: str | None = None,
    ):
        super().__init__()
        self._notes_list = notes_list
        self._note_details = note_details
        self._fail_id = fail_id

    def send(self, request: Any, **kwargs: Any) -> requests.Response:
        resp = requests.Response()
        resp.request = request

        # Detail endpoint: /v1/notes/{id}
        for nid, detail in self._note_details.items():
            if f"/v1/notes/{nid}" in request.url:
                if nid == self._fail_id:
                    resp.status_code = 500
                    resp._content = b'{"error": "internal error"}'
                    return resp
                resp.status_code = 200
                resp._content = json.dumps(detail).encode("utf-8")
                return resp

        # List endpoint: /v1/notes
        if "/v1/notes" in request.url:
            resp.status_code = 200
            resp._content = json.dumps(
                {"notes": self._notes_list, "hasMore": False, "cursor": None}
            ).encode("utf-8")
            return resp

        resp.status_code = 404
        resp._content = b'{"error": "not found"}'
        return resp


def _build_test_session(
    notes_list: list[dict],
    note_details: dict[str, dict],
    fail_id: str | None = None,
) -> requests.Session:
    session = requests.Session()
    session.mount(
        "https://public-api.granola.ai",
        MockGranolaTransport(notes_list, note_details, fail_id),
    )
    return session


def _run_pipeline(tmp_path: Any, source: Any) -> Any:
    import dlt

    db_path = (tmp_path / "granola_test.db").as_posix()
    pipeline = dlt.pipeline(
        pipeline_name="granola_test_pipeline",
        destination=dlt.destinations.sqlalchemy(f"sqlite:///{db_path}"),
        dataset_name="granola_ds",
        pipelines_dir=str(tmp_path / "state"),
    )
    pipeline.run(source)
    return pipeline


def _fetch_rows(pipeline: Any) -> dict[str, dict[str, Any]]:
    with (
        pipeline.sql_client() as client,
        client.execute_query(f"SELECT id, title, url, content FROM {GRANOLA_TABLE_NAME}") as cursor,
    ):
        raw_rows = cursor.fetchall()
    return {
        row[1]: {"id": row[0], "title": row[1], "url": row[2], "content": row[3]}
        for row in raw_rows
    }


def test_first_sync_loads_notes_with_rendered_content(tmp_path):
    notes_list = [
        {"id": "not_1", "title": "Alpha Meeting", "deleted_at": None},
    ]
    note_details = {
        "not_1": {
            "id": "not_1",
            "title": "Alpha Meeting",
            "web_url": "https://notes.granola.ai/d/alpha",
            "summary_markdown": "Discussed sprint goals.",
            "transcript": [
                {
                    "speaker": {"name": "Bob", "attribution": "me"},
                    "text": "Sprint goal is delivered.",
                }
            ],
        }
    }
    session = _build_test_session(notes_list, note_details)
    source = granola_source(api_key="grn_test", client=session)

    pipeline = _run_pipeline(tmp_path, source)
    rows = _fetch_rows(pipeline)

    assert "Alpha Meeting" in rows
    assert "Discussed sprint goals." in rows["Alpha Meeting"]["content"]
    assert "Sprint goal is delivered." in rows["Alpha Meeting"]["content"]


def test_edit_is_reflected_on_resync(tmp_path):
    notes_list = [{"id": "not_1", "title": "Alpha Meeting", "deleted_at": None}]
    details_v1 = {
        "not_1": {
            "id": "not_1",
            "title": "Alpha Meeting",
            "summary_markdown": "Version 1 notes.",
        }
    }
    pipeline = _run_pipeline(
        tmp_path,
        granola_source(api_key="grn_test", client=_build_test_session(notes_list, details_v1)),
    )
    rows = _fetch_rows(pipeline)
    assert "Version 1 notes." in rows["Alpha Meeting"]["content"]

    # Re-sync with edited content
    details_v2 = {
        "not_1": {
            "id": "not_1",
            "title": "Alpha Meeting",
            "summary_markdown": "Version 2 updated notes.",
        }
    }
    pipeline = _run_pipeline(
        tmp_path,
        granola_source(api_key="grn_test", client=_build_test_session(notes_list, details_v2)),
    )
    rows = _fetch_rows(pipeline)
    assert "Version 2 updated notes." in rows["Alpha Meeting"]["content"]
    assert "Version 1 notes." not in rows["Alpha Meeting"]["content"]


def test_vanished_note_is_removed_on_resync(tmp_path):
    notes_list = [
        {"id": "not_1", "title": "Alpha Meeting", "deleted_at": None},
        {"id": "not_2", "title": "Beta Meeting", "deleted_at": None},
    ]
    details = {
        "not_1": {"id": "not_1", "title": "Alpha Meeting", "summary_markdown": "A"},
        "not_2": {"id": "not_2", "title": "Beta Meeting", "summary_markdown": "B"},
    }
    pipeline = _run_pipeline(
        tmp_path,
        granola_source(api_key="grn_test", client=_build_test_session(notes_list, details)),
    )
    assert len(_fetch_rows(pipeline)) == 2

    # not_2 disappears from API listing (vanished note)
    resync_notes = [{"id": "not_1", "title": "Alpha Meeting", "deleted_at": None}]
    pipeline = _run_pipeline(
        tmp_path,
        granola_source(api_key="grn_test", client=_build_test_session(resync_notes, details)),
    )
    rows = _fetch_rows(pipeline)
    assert "Alpha Meeting" in rows
    assert "Beta Meeting" not in rows


def test_deleted_at_note_is_filtered_on_resync(tmp_path):
    notes_list = [
        {"id": "not_1", "title": "Alpha Meeting", "deleted_at": None},
        {"id": "not_2", "title": "Beta Meeting", "deleted_at": None},
    ]
    details = {
        "not_1": {"id": "not_1", "title": "Alpha Meeting", "summary_markdown": "A"},
        "not_2": {"id": "not_2", "title": "Beta Meeting", "summary_markdown": "B"},
    }
    pipeline = _run_pipeline(
        tmp_path,
        granola_source(api_key="grn_test", client=_build_test_session(notes_list, details)),
    )
    assert len(_fetch_rows(pipeline)) == 2

    # Workspace API key: note remains in list but has deleted_at populated
    resync_notes = [
        {"id": "not_1", "title": "Alpha Meeting", "deleted_at": None},
        {"id": "not_2", "title": "Beta Meeting", "deleted_at": "2026-02-01T12:00:00Z"},
    ]
    pipeline = _run_pipeline(
        tmp_path,
        granola_source(api_key="grn_test", client=_build_test_session(resync_notes, details)),
    )
    rows = _fetch_rows(pipeline)
    assert "Alpha Meeting" in rows
    assert "Beta Meeting" not in rows


def test_api_error_aborts_run_staging_untouched(tmp_path):
    notes_list = [{"id": "not_1", "title": "Alpha Meeting", "deleted_at": None}]
    details = {"not_1": {"id": "not_1", "title": "Alpha Meeting", "summary_markdown": "A"}}

    # Successful initial sync
    pipeline = _run_pipeline(
        tmp_path,
        granola_source(api_key="grn_test", client=_build_test_session(notes_list, details)),
    )
    assert len(_fetch_rows(pipeline)) == 1

    # Second sync encounters server error on detail endpoint
    fail_session = _build_test_session(notes_list, details, fail_id="not_1")
    fail_source = granola_source(api_key="grn_test", client=fail_session)

    with pytest.raises(Exception):  # noqa: B017 - dlt wraps the source error in PipelineStepFailed
        pipeline.run(fail_source)

    # Staging must remain untouched — live notes are not wiped out
    rows_after = _fetch_rows(pipeline)
    assert len(rows_after) == 1
    assert "Alpha Meeting" in rows_after
