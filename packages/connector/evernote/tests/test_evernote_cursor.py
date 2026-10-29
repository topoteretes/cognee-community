"""Unit tests for Evernote incremental sync cursor and resource_state persistence.

Tests verify after_usn cursor progression, state persistence across sync runs,
handling of no-change runs, retry safety on errors, scope filtering, and tombstone tracking.
"""

from types import SimpleNamespace

import dlt
import pytest
from evernote.edam.notestore.ttypes import SyncChunk, SyncState

from cognee_community_connector_evernote import (
    EvernoteAuthError,
    EvernoteRateLimitError,
    evernote_source,
)
from cognee_community_connector_evernote.evernote import _sync_records


class FakeCursorEvernoteClient:
    def __init__(
        self,
        chunks=None,
        note_contents=None,
        update_count=100,
        current_time=1609459200000,
    ):
        self.chunks = list(chunks or [])
        self.note_contents = dict(note_contents or {})
        self.update_count = update_count
        self.current_time = current_time
        self.get_filtered_sync_chunk_calls = []
        self.get_note_content_calls = []
        self.get_sync_state_calls = 0

    def get_sync_state(self):
        self.get_sync_state_calls += 1
        return SyncState(
            currentTime=self.current_time,
            fullSyncBefore=0,
            updateCount=self.update_count,
        )

    def get_user_id(self):
        return 12345

    def get_filtered_sync_chunk(self, after_usn, max_entries, filter_obj):
        self.get_filtered_sync_chunk_calls.append((after_usn, max_entries, filter_obj))
        if self.chunks:
            return self.chunks.pop(0)
        return SyncChunk(chunkHighUSN=None)

    def get_note_content(self, guid):
        self.get_note_content_calls.append(guid)
        if guid in self.note_contents:
            val = self.note_contents[guid]
            if isinstance(val, Exception):
                raise val
            return val
        return f"<en-note><div>Body for {guid}</div></en-note>"


def test_first_run_starts_from_after_usn_zero_and_stores_state():
    """First run starts from afterUSN 0, stores cursor, maps, and note_ids."""
    nb = SimpleNamespace(guid="nb-1", name="Engineering")
    tag = SimpleNamespace(guid="t-1", name="Urgent")
    note1 = SimpleNamespace(
        guid="n-1",
        title="Note 1",
        notebookGuid="nb-1",
        tagGuids=["t-1"],
        active=True,
    )
    note2 = SimpleNamespace(
        guid="n-2",
        title="Note 2",
        notebookGuid="nb-1",
        tagGuids=[],
        active=True,
    )

    chunk = SyncChunk(
        chunkHighUSN=50,
        updateCount=50,
        notebooks=[nb],
        tags=[tag],
        notes=[note1, note2],
    )
    client = FakeCursorEvernoteClient(
        chunks=[chunk],
        update_count=50,
        current_time=1609459200000,
    )

    state: dict = {}
    rows = list(_sync_records(client, state))

    assert len(rows) == 2
    assert len(client.get_filtered_sync_chunk_calls) == 1
    after_usn, _, _ = client.get_filtered_sync_chunk_calls[0]
    assert after_usn == 0

    assert state["last_usn"] == 50
    assert state["last_sync_time"] == 1609459200000
    assert state["notebooks"] == {"nb-1": "Engineering"}
    assert state["tags"] == {"t-1": "Urgent"}
    assert state["note_ids"] == ["n-1", "n-2"]


def test_second_run_with_no_server_changes_makes_no_chunk_call_and_yields_no_rows():
    """Second run with no server changes makes no chunk call, yields no rows, updates sync time."""
    client = FakeCursorEvernoteClient(update_count=50, current_time=1609500000000)
    state = {
        "last_usn": 50,
        "last_sync_time": 1609459200000,
        "notebooks": {"nb-1": "Engineering"},
        "tags": {},
        "note_ids": ["n-1"],
    }

    rows = list(_sync_records(client, state))

    # Yields only schema materialization marker, no data rows
    data_rows = [r for r in rows if isinstance(r, dict)]
    assert data_rows == []
    # No chunk calls made
    assert client.get_filtered_sync_chunk_calls == []
    # last_usn unchanged, last_sync_time updated
    assert state["last_usn"] == 50
    assert state["last_sync_time"] == 1609500000000


def test_second_run_after_changes_calls_get_filtered_sync_chunk_with_after_usn():
    """Second run after changes calls getFilteredSyncChunk with afterUSN == stored last_usn."""
    nb = SimpleNamespace(guid="nb-1", name="Engineering")
    note_new = SimpleNamespace(guid="n-new", title="New Note", notebookGuid="nb-1", active=True)
    chunk = SyncChunk(
        chunkHighUSN=75,
        updateCount=75,
        notebooks=[nb],
        notes=[note_new],
    )
    client = FakeCursorEvernoteClient(chunks=[chunk], update_count=75)
    state = {
        "last_usn": 50,
        "last_sync_time": 1000,
        "notebooks": {"nb-1": "Engineering"},
        "tags": {},
        "note_ids": ["n-old"],
    }

    rows = list(_sync_records(client, state))

    assert len(rows) == 1
    assert rows[0]["id"] == "n-new"
    assert len(client.get_filtered_sync_chunk_calls) == 1
    after_usn, _, _ = client.get_filtered_sync_chunk_calls[0]
    assert after_usn == 50
    assert state["last_usn"] == 75
    assert state["note_ids"] == ["n-new", "n-old"]


def test_only_changed_notes_have_body_fetched():
    """Only changed notes present in the incremental chunk have their bodies fetched."""
    note_changed = SimpleNamespace(
        guid="n-changed", title="Changed Note", notebookGuid="nb-1", active=True
    )
    chunk = SyncChunk(
        chunkHighUSN=80,
        updateCount=100,
        notes=[note_changed],
    )
    client = FakeCursorEvernoteClient(chunks=[chunk], update_count=100)
    state = {
        "last_usn": 50,
        "notebooks": {"nb-1": "Engineering"},
        "tags": {},
        "note_ids": ["n-existing"],
    }

    list(_sync_records(client, state))

    # Only n-changed had its body fetched; n-existing was not touched
    assert client.get_note_content_calls == ["n-changed"]


def test_changed_note_resolves_names_from_persisted_maps():
    """A changed note whose notebook/tag was NOT in the new chunk still gets names from state."""
    note_changed = SimpleNamespace(
        guid="n-changed",
        title="Note in Existing NB",
        notebookGuid="nb-old",
        tagGuids=["t-old"],
        active=True,
    )
    chunk = SyncChunk(
        chunkHighUSN=90,
        updateCount=100,
        notebooks=[],
        tags=[],
        notes=[note_changed],
    )
    client = FakeCursorEvernoteClient(chunks=[chunk], update_count=100)
    state = {
        "last_usn": 50,
        "notebooks": {"nb-old": "Persisted Notebook"},
        "tags": {"t-old": "Persisted Tag"},
        "note_ids": [],
    }

    rows = list(_sync_records(client, state))

    assert len(rows) == 1
    assert rows[0]["notebook"] == "Persisted Notebook"
    assert rows[0]["tags"] == "Persisted Tag"


def test_multichunk_incremental_pagination_advances_cursor_to_last_chunk():
    """Multi-chunk incremental pagination advances the cursor to the highest chunk."""
    chunk1 = SyncChunk(
        chunkHighUSN=60,
        updateCount=100,
        notes=[SimpleNamespace(guid="n-1", title="N1", active=True)],
    )
    chunk2 = SyncChunk(
        chunkHighUSN=80,
        updateCount=100,
        notes=[SimpleNamespace(guid="n-2", title="N2", active=True)],
    )
    chunk3 = SyncChunk(
        chunkHighUSN=100,
        updateCount=100,
        notes=[SimpleNamespace(guid="n-3", title="N3", active=True)],
    )
    client = FakeCursorEvernoteClient(chunks=[chunk1, chunk2, chunk3], update_count=100)
    state = {"last_usn": 40}

    rows = list(_sync_records(client, state))

    assert len(rows) == 3
    assert state["last_usn"] == 100
    assert state["note_ids"] == ["n-1", "n-2", "n-3"]


def test_trashed_and_expunged_notes_yield_tombstones_and_leave_note_ids():
    """Trashed and expunged notes yield tombstones and are removed from note_ids."""
    note_trashed = SimpleNamespace(guid="n-trashed", title="T", active=False)
    chunk = SyncChunk(
        chunkHighUSN=60,
        updateCount=100,
        notes=[note_trashed],
        expungedNotes=["n-expunged"],
    )
    client = FakeCursorEvernoteClient(chunks=[chunk], update_count=100)
    state = {
        "last_usn": 40,
        "note_ids": ["n-expunged", "n-survivor", "n-trashed"],
    }

    rows = list(_sync_records(client, state))

    tombstone_ids = {r["id"] for r in rows if r.get("_deleted")}
    assert tombstone_ids == {"n-trashed", "n-expunged"}
    # Only the survivor remains in note_ids
    assert state["note_ids"] == ["n-survivor"]


def test_note_moved_out_of_scope_yields_tombstone():
    """A note moved to an out-of-scope notebook yields a tombstone and is removed from note_ids."""
    nb1 = SimpleNamespace(guid="nb-in", name="InScope")
    nb2 = SimpleNamespace(guid="nb-out", name="OutOfScope")
    note_moved = SimpleNamespace(
        guid="n-moved",
        title="Moved Note",
        notebookGuid="nb-out",
        active=True,
    )
    chunk = SyncChunk(
        chunkHighUSN=60,
        updateCount=100,
        notebooks=[nb1, nb2],
        notes=[note_moved],
    )
    client = FakeCursorEvernoteClient(chunks=[chunk], update_count=100)
    state = {
        "last_usn": 40,
        "notebooks": {"nb-in": "InScope", "nb-out": "OutOfScope"},
        "note_ids": ["n-moved"],
    }

    rows = list(_sync_records(client, state, notebook_names=["InScope"]))

    assert len(rows) == 1
    assert rows[0]["id"] == "n-moved"
    assert rows[0]["_deleted"] is True
    assert state["note_ids"] == []


def test_body_fetch_failure_mid_run_leaves_cursor_unchanged_and_retries_safely():
    """Body fetch failure mid-run leaves cursor unchanged; next run processes the changes."""
    note1 = SimpleNamespace(guid="n-ok", title="OK Note", active=True)
    note2 = SimpleNamespace(guid="n-fail", title="Fail Note", active=True)
    chunk = SyncChunk(
        chunkHighUSN=60,
        updateCount=100,
        notes=[note1, note2],
    )
    client_failed = FakeCursorEvernoteClient(
        chunks=[chunk],
        note_contents={"n-fail": RuntimeError("Connection reset during body fetch")},
        update_count=100,
    )
    state = {"last_usn": 40, "note_ids": []}

    # First attempt fails mid-run
    with pytest.raises(RuntimeError, match="Connection reset"):
        list(_sync_records(client_failed, state))

    # Cursor and state remain completely untouched
    assert state["last_usn"] == 40
    assert state["note_ids"] == []

    # Second attempt succeeds
    chunk_retry = SyncChunk(
        chunkHighUSN=60,
        updateCount=100,
        notes=[note1, note2],
    )
    client_retry = FakeCursorEvernoteClient(
        chunks=[chunk_retry],
        note_contents={"n-fail": "<en-note><div>Recovered body</div></en-note>"},
        update_count=100,
    )
    rows = list(_sync_records(client_retry, state))

    assert len(rows) == 2
    assert state["last_usn"] == 60
    assert state["note_ids"] == ["n-fail", "n-ok"]


def test_rate_limit_and_auth_error_leave_cursor_unchanged():
    """Rate limit exhaustion or auth error leaves the cursor untouched."""
    note = SimpleNamespace(guid="n-1", title="N1", active=True)
    chunk = SyncChunk(chunkHighUSN=60, updateCount=100, notes=[note])

    # 1. Rate limit error during chunk iteration
    client_rate_limited = FakeCursorEvernoteClient(chunks=[chunk], update_count=100)
    client_rate_limited.get_filtered_sync_chunk = lambda *_: (_ for _ in ()).throw(
        EvernoteRateLimitError("Rate limit exceeded")
    )
    state = {"last_usn": 40, "note_ids": []}

    with pytest.raises(EvernoteRateLimitError):
        list(_sync_records(client_rate_limited, state))
    assert state["last_usn"] == 40

    # 2. Auth error during sync state check
    client_auth = FakeCursorEvernoteClient(update_count=100)
    client_auth.get_sync_state = lambda: (_ for _ in ()).throw(EvernoteAuthError("Token expired"))

    with pytest.raises(EvernoteAuthError):
        list(_sync_records(client_auth, state))
    assert state["last_usn"] == 40


def test_server_update_count_less_than_last_usn_triggers_full_resync():
    """When server updateCount < last_usn, trigger a full resync starting from USN 0."""
    chunk = SyncChunk(
        chunkHighUSN=30,
        updateCount=30,
        notes=[SimpleNamespace(guid="n-resync", title="Resynced", active=True)],
    )
    client = FakeCursorEvernoteClient(chunks=[chunk], update_count=30)
    state = {"last_usn": 50, "note_ids": ["n-old"]}

    rows = list(_sync_records(client, state))

    # Calls get_filtered_sync_chunk with after_usn == 0 (from USN 0)
    assert client.get_filtered_sync_chunk_calls[0][0] == 0
    # Stale note from state["note_ids"] is tombstoned
    tombstones = [r for r in rows if r.get("_deleted")]
    assert len(tombstones) == 1
    assert tombstones[0]["id"] == "n-old"
    # Live note yielded as active row
    active = [r for r in rows if not r.get("_deleted")]
    assert len(active) == 1
    assert active[0]["id"] == "n-resync"
    # New last_usn is stored
    assert state["last_usn"] == 30
    assert state["note_ids"] == ["n-resync"]


def test_dlt_pipeline_real_persistence_across_runs(tmp_path):
    """Verify that a real dlt pipeline with sqlite destination persists state across runs."""
    nb = SimpleNamespace(guid="nb-1", name="Engineering")
    note1 = SimpleNamespace(guid="n-1", title="First Note", notebookGuid="nb-1", active=True)
    chunk1 = SyncChunk(
        chunkHighUSN=50,
        updateCount=50,
        notebooks=[nb],
        notes=[note1],
    )
    client = FakeCursorEvernoteClient(
        chunks=[chunk1],
        update_count=50,
        current_time=1609459200000,
    )

    pipeline = dlt.pipeline(
        pipeline_name="evernote_cursor_test",
        destination=dlt.destinations.sqlalchemy(f"sqlite:///{tmp_path}/data.db"),
        dataset_name="notes",
        pipelines_dir=str(tmp_path / "state"),
    )

    # Run 1: initial ingestion
    source1 = evernote_source(token="fake-tok", client=client)
    pipeline.run(source1, write_disposition="merge")

    # Read state from pipeline
    sources_state = pipeline.state.get("sources", {}).get("evernote_cursor_test", {})
    pipe_state = sources_state.get("resources", {}).get("evernote_notes", {})
    assert pipe_state.get("last_usn") == 50
    assert pipe_state.get("note_ids") == ["n-1"]

    # Run 2: no changes (server updateCount == 50)
    client.update_count = 50
    client.current_time = 1609500000000
    source2 = evernote_source(token="fake-tok", client=client)
    pipeline.run(source2, write_disposition="merge")

    sources_state2 = pipeline.state.get("sources", {}).get("evernote_cursor_test", {})
    pipe_state2 = sources_state2.get("resources", {}).get("evernote_notes", {})
    assert pipe_state2.get("last_usn") == 50
    assert pipe_state2.get("last_sync_time") == 1609500000000
    # No chunk call was made during run 2
    assert len(client.get_filtered_sync_chunk_calls) == 1  # only run 1's call
