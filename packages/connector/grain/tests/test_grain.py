"""Unit tests for the Grain connector (no live token or network required)."""

from __future__ import annotations

from copy import deepcopy

import pytest

from cognee_community_connector_grain.grain import (
    GRAIN_API_VERSION,
    HIGHLIGHT_SEARCH_WEIGHT,
    TRANSCRIPT_SEARCH_WEIGHT,
    _format_milliseconds,
    _iter_recordings,
    _make_session,
    _recording_created_at,
    _render_transcript,
    _selected_content,
    grain_source,
    sync_grain,
)


def _highlight(highlight_id="h1", text="Customer loves it", transcript="This solves it"):
    return {
        "id": highlight_id,
        "recording_id": "r1",
        "text": text,
        "transcript": transcript,
        "speakers": ["Leia"],
        "timestamp": 65_000,
        "duration": 5_000,
        "tags": ["customer-signal"],
        "url": f"https://grain.com/share/highlight/{highlight_id}",
        "created_datetime": "2026-01-01T10:15:00Z",
    }


def _recording(
    recording_id="r1",
    *,
    title="Customer call",
    created_at="2026-01-01T10:00:00Z",
    highlights=None,
):
    return {
        "id": recording_id,
        "title": title,
        "created_at": created_at,
        "start_datetime": created_at,
        "end_datetime": "2026-01-01T10:30:00Z",
        "duration_ms": 1_800_000,
        "media_type": "video",
        "source": "zoom",
        "url": f"https://grain.com/share/recording/{recording_id}",
        "participants": [{"name": "Leia"}, {"name": "Han"}],
        "tags": ["sales"],
        "highlights": list(highlights if highlights is not None else [_highlight()]),
    }


class FakeResponse:
    def __init__(self, payload, status_code=200, headers=None):
        self.payload = payload
        self.status_code = status_code
        self.headers = headers or {}

    def json(self):
        return deepcopy(self.payload)

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class FakeGrainSession:
    """Minimal requests.Session stand-in with mutable Grain fixtures."""

    def __init__(self, recordings, transcripts=None, page_size=None):
        self.recordings = recordings
        self.transcripts = transcripts or {}
        self.page_size = page_size
        self.requests = []
        self.transcript_calls = []

    def request(self, method, url, json=None, timeout=None):
        self.requests.append((method, url, deepcopy(json), timeout))
        if method == "GET" and url.endswith("/transcript"):
            recording_id = url.rsplit("/", 2)[-2]
            self.transcript_calls.append(recording_id)
            return FakeResponse(self.transcripts.get(recording_id, []))

        assert method == "POST"
        assert url.endswith("/recordings")
        body = json or {}
        offset = int(body.get("cursor") or 0)
        page_size = self.page_size or max(1, len(self.recordings))
        page = self.recordings[offset : offset + page_size]
        next_offset = offset + page_size
        cursor = str(next_offset) if next_offset < len(self.recordings) else None
        return FakeResponse({"recordings": page, "cursor": cursor})


TRANSCRIPT = [
    {"start": 8_000, "end": 9_000, "speaker": "Leia", "text": "We need a plan."},
    {"start": 11_000, "end": 13_000, "speaker": "Han", "text": "I have one."},
]


def test_timestamp_and_transcript_rendering():
    assert _format_milliseconds(3_661_000) == "01:01:01"
    assert _format_milliseconds(None) == "00:00:00"
    assert _render_transcript(TRANSCRIPT) == (
        "[00:00:08] Leia: We need a plan.\n[00:00:11] Han: I have one."
    )


def test_creation_cursor_prefers_creation_fields_and_falls_back_to_start():
    assert _recording_created_at({"created_at": "a", "start_datetime": "c"}) == "a"
    assert _recording_created_at({"created_datetime": "b", "start_datetime": "c"}) == "b"
    assert _recording_created_at({"start_datetime": "c"}) == "c"


def test_content_selection_validation():
    assert _selected_content("highlights") == {"highlights"}
    assert _selected_content(["recordings", "transcripts"]) == {
        "recordings",
        "transcripts",
    }
    with pytest.raises(ValueError, match="at least one"):
        _selected_content([])
    with pytest.raises(ValueError, match="Unknown Grain content"):
        _selected_content(["stories"])


def test_make_session_sets_bearer_and_current_api_version():
    session = _make_session("secret-token")
    assert session.headers["Authorization"] == "Bearer secret-token"
    assert session.headers["Public-Api-Version"] == GRAIN_API_VERSION


def test_recording_pagination_passes_cursor_and_include_flags():
    session = FakeGrainSession([_recording("r1"), _recording("r2"), _recording("r3")], page_size=1)
    rows = list(
        _iter_recordings(
            session,
            include_highlights=True,
            include_participants=False,
            recording_filter={"team": "team-1"},
        )
    )

    assert [row["id"] for row in rows] == ["r1", "r2", "r3"]
    request_bodies = [request[2] for request in session.requests]
    assert request_bodies[0] == {
        "filter": {"team": "team-1"},
        "include": {"highlights": True},
    }
    assert [body.get("cursor") for body in request_bodies] == [None, "1", "2"]


def test_initial_sync_ingests_all_selected_content_and_records_cursor():
    session = FakeGrainSession([_recording()], {"r1": TRANSCRIPT})
    state = {}

    rows = list(sync_grain(session, state))

    assert {row["id"] for row in rows} == {"recording:r1", "transcript:r1", "highlight:h1"}
    assert session.transcript_calls == ["r1"]
    assert state["last_created_at"] == "2026-01-01T10:00:00Z"
    assert state["known_recording_ids"] == ["r1"]
    assert set(state["document_fingerprints"]) == {
        "recording:r1",
        "transcript:r1",
        "highlight:h1",
    }

    transcript = next(row for row in rows if row["id"] == "transcript:r1")
    highlight = next(row for row in rows if row["id"] == "highlight:h1")
    assert transcript["search_weight"] == TRANSCRIPT_SEARCH_WEIGHT
    assert highlight["search_weight"] == HIGHLIGHT_SEARCH_WEIGHT
    assert HIGHLIGHT_SEARCH_WEIGHT > TRANSCRIPT_SEARCH_WEIGHT
    assert "Human-curated" in highlight["content"]
    assert "This solves it" in highlight["content"]


def test_incremental_sync_is_noop_for_unchanged_documents_and_skips_transcript_fetch():
    session = FakeGrainSession([_recording()], {"r1": TRANSCRIPT})
    state = {}
    list(sync_grain(session, state))
    session.transcript_calls.clear()

    rows = list(sync_grain(session, state))

    assert rows == []
    assert session.transcript_calls == []


def test_incremental_sync_fetches_only_new_recording_since_cursor():
    first = _recording("r1", highlights=[])
    session = FakeGrainSession([first], {"r1": TRANSCRIPT, "r2": TRANSCRIPT})
    state = {}
    list(sync_grain(session, state))
    session.transcript_calls.clear()

    second = _recording(
        "r2",
        title="Product sync",
        created_at="2026-02-01T09:00:00Z",
        highlights=[],
    )
    session.recordings = [first, second]
    rows = list(sync_grain(session, state))

    assert {row["id"] for row in rows} == {"recording:r2", "transcript:r2"}
    assert session.transcript_calls == ["r2"]
    assert state["last_created_at"] == "2026-02-01T09:00:00Z"


def test_changed_highlight_is_reingested_without_refetching_transcript():
    session = FakeGrainSession([_recording()], {"r1": TRANSCRIPT})
    state = {}
    list(sync_grain(session, state))
    session.transcript_calls.clear()

    session.recordings[0]["highlights"][0]["text"] = "Updated curated insight"
    rows = list(sync_grain(session, state))

    assert [row["id"] for row in rows] == ["highlight:h1"]
    assert "Updated curated insight" in rows[0]["content"]
    assert session.transcript_calls == []


def test_highlights_only_selection_never_fetches_transcript():
    session = FakeGrainSession([_recording()], {"r1": TRANSCRIPT})
    state = {}
    rows = list(sync_grain(session, state, content_types=["highlights"]))

    assert [row["id"] for row in rows] == ["highlight:h1"]
    assert session.transcript_calls == []
    list_request = session.requests[0][2]
    assert list_request["include"] == {"highlights": True}


def test_recording_id_allowlist_limits_ingestion():
    session = FakeGrainSession([_recording("r1"), _recording("r2")])
    rows = list(
        sync_grain(
            session,
            {},
            content_types=["recordings"],
            recording_ids=["r2"],
        )
    )
    assert [row["id"] for row in rows] == ["recording:r2"]


def test_upstream_recording_delete_emits_tombstones_for_all_derived_documents():
    session = FakeGrainSession(
        [_recording("r1"), _recording("r2", highlights=[])],
        {"r1": TRANSCRIPT, "r2": TRANSCRIPT},
    )
    state = {}
    list(sync_grain(session, state))

    session.recordings = [session.recordings[1]]
    rows = list(sync_grain(session, state))

    assert rows == [
        {"id": "highlight:h1", "_deleted": True},
        {"id": "recording:r1", "_deleted": True},
        {"id": "transcript:r1", "_deleted": True},
    ]
    assert state["known_recording_ids"] == ["r2"]


def test_upstream_highlight_delete_emits_its_own_tombstone():
    session = FakeGrainSession([_recording()], {"r1": TRANSCRIPT})
    state = {}
    list(sync_grain(session, state))
    session.transcript_calls.clear()

    session.recordings[0]["highlights"] = []
    rows = list(sync_grain(session, state))

    assert rows == [{"id": "highlight:h1", "_deleted": True}]
    assert session.transcript_calls == []


def test_dlt_merge_physically_removes_deleted_recording(tmp_path):
    """Prove the emitted hard-delete marker works in dlt, not only in a helper."""
    import dlt

    session = FakeGrainSession([_recording("r1"), _recording("r2")])
    database_path = (tmp_path / "grain.db").as_posix()
    pipeline = dlt.pipeline(
        pipeline_name="grain_test",
        destination=dlt.destinations.sqlalchemy(f"sqlite:///{database_path}"),
        dataset_name="grain_ds",
        pipelines_dir=str(tmp_path / "state"),
    )

    pipeline.run(grain_source(session=session, content_types=["recordings"]))
    with (
        pipeline.sql_client() as client,
        client.execute_query("SELECT id FROM grain_content ORDER BY id") as cursor,
    ):
        assert [row[0] for row in cursor.fetchall()] == ["recording:r1", "recording:r2"]

    session.recordings = [session.recordings[1]]
    pipeline.run(grain_source(session=session, content_types=["recordings"]))
    with (
        pipeline.sql_client() as client,
        client.execute_query("SELECT id FROM grain_content ORDER BY id") as cursor,
    ):
        assert [row[0] for row in cursor.fetchall()] == ["recording:r2"]


def test_grain_source_declares_document_mode_merge_and_hard_delete():
    from cognee.tasks.ingestion.dlt_utils import document_source_tag

    resource = grain_source(session=FakeGrainSession([]), content_types="highlights")
    assert document_source_tag(resource) == "grain"

    schema = resource.compute_table_schema()
    write_disposition = schema["write_disposition"]
    if isinstance(write_disposition, dict):
        write_disposition = write_disposition.get("disposition")
    assert write_disposition == "merge"
    assert schema.get("primary_key") == ["id"] or schema["columns"]["id"].get("primary_key")
    assert schema["columns"]["_deleted"]["hard_delete"] is True


def test_grain_source_requires_token_without_injected_session(monkeypatch):
    monkeypatch.delenv("GRAIN_API_TOKEN", raising=False)
    with pytest.raises(ValueError, match="Grain API token required"):
        grain_source(content_types="recordings")
