"""Unit tests for Evernote full resync, deletion reconciliation, and account binding.

Tests verify:
- fullSyncBefore newer than last_sync_time triggers a from-0 read and tombstones stale notes
- full_resync=True triggers full resync and tombstones stale notes
- updateCount < last_usn triggers a full resync
- only IDs previously in state["note_ids"] are tombstoned
- unknown notebook name raises before any tombstone and leaves state untouched
- failure during pass 1 yields nothing and leaves state unchanged
- account mismatch raises before chunk calls with state untouched
- first run reconciles nothing (zero tombstones)
- maps are rebuilt on full resync (renamed notebook gets new name)
- expunged notebook and tag GUIDs are dropped from persisted maps
- resync followed by an incremental run uses the new cursor
- getSyncState().currentTime is None leaves state["last_sync_time"] unchanged
"""

from types import SimpleNamespace

import pytest
from evernote.edam.notestore.ttypes import SyncChunk, SyncState

from cognee_community_connector_evernote import EvernoteError, evernote_source
from cognee_community_connector_evernote.evernote import _sync_records


class FakeResyncEvernoteClient:
    def __init__(
        self,
        chunks=None,
        note_contents=None,
        update_count=100,
        current_time=1609459200000,
        full_sync_before=0,
        user_id=12345,
    ):
        self.chunks = list(chunks or [])
        self.note_contents = dict(note_contents or {})
        self.update_count = update_count
        self.current_time = current_time
        self.full_sync_before = full_sync_before
        self.user_id = user_id
        self.get_filtered_sync_chunk_calls = []
        self.get_note_content_calls = []
        self.get_sync_state_calls = 0

    def get_user_id(self):
        return self.user_id

    def get_sync_state(self):
        self.get_sync_state_calls += 1
        return SyncState(
            currentTime=self.current_time,
            fullSyncBefore=self.full_sync_before,
            updateCount=self.update_count,
        )

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


def test_full_sync_before_newer_than_last_sync_time_triggers_from_zero_read():
    """fullSyncBefore newer than last_sync_time triggers a from-0 read.

    Stale notes are tombstoned during deletion reconciliation.
    """
    nb = SimpleNamespace(guid="nb-1", name="Engineering")
    note_live = SimpleNamespace(guid="n-live", title="Live Note", notebookGuid="nb-1", active=True)
    chunk = SyncChunk(
        chunkHighUSN=60,
        updateCount=60,
        notebooks=[nb],
        notes=[note_live],
    )
    # Server cutoff fullSyncBefore is 2000, last_sync_time was 1000 -> full resync needed
    client = FakeResyncEvernoteClient(
        chunks=[chunk],
        update_count=60,
        current_time=3000,
        full_sync_before=2000,
    )
    state = {
        "last_usn": 50,
        "last_sync_time": 1000,
        "note_ids": ["n-live", "n-stale"],
        "account": "production:12345",
    }

    rows = list(_sync_records(client, state))

    # Read started from USN 0
    assert client.get_filtered_sync_chunk_calls[0][0] == 0

    # n-stale was not in the live notes from USN 0 -> tombstone yielded
    tombstones = [r for r in rows if r.get("_deleted")]
    assert len(tombstones) == 1
    assert tombstones[0]["id"] == "n-stale"

    # n-live was yielded as active row
    active = [r for r in rows if not r.get("_deleted")]
    assert len(active) == 1
    assert active[0]["id"] == "n-live"

    # State updated with new cursor, sync time, and note_ids
    assert state["last_usn"] == 60
    assert state["last_sync_time"] == 3000
    assert state["note_ids"] == ["n-live"]


def test_full_resync_flag_true_triggers_from_zero_read_and_tombstones_stale_note():
    """full_resync=True triggers read from USN 0 and tombstones stale notes."""
    nb = SimpleNamespace(guid="nb-1", name="Engineering")
    note_live = SimpleNamespace(guid="n-live", title="Live Note", notebookGuid="nb-1", active=True)
    chunk = SyncChunk(
        chunkHighUSN=50,
        updateCount=50,
        notebooks=[nb],
        notes=[note_live],
    )
    client = FakeResyncEvernoteClient(
        chunks=[chunk],
        update_count=50,
        current_time=2000,
        full_sync_before=500,
    )
    state = {
        "last_usn": 50,
        "last_sync_time": 1000,
        "note_ids": ["n-live", "n-stale"],
        "account": "production:12345",
    }

    # Pass full_resync=True
    rows = list(_sync_records(client, state, full_resync=True))

    assert client.get_filtered_sync_chunk_calls[0][0] == 0
    tombstones = [r for r in rows if r.get("_deleted")]
    assert len(tombstones) == 1
    assert tombstones[0]["id"] == "n-stale"
    assert state["note_ids"] == ["n-live"]


def test_server_update_count_less_than_last_usn_triggers_full_resync():
    """updateCount < last_usn triggers a full resync from USN 0 instead of raising."""
    note_new = SimpleNamespace(guid="n-new", title="New Note", active=True)
    chunk = SyncChunk(
        chunkHighUSN=20,
        updateCount=20,
        notes=[note_new],
    )
    # Server updateCount is 20, but state had last_usn=50
    client = FakeResyncEvernoteClient(chunks=[chunk], update_count=20)
    state = {
        "last_usn": 50,
        "note_ids": ["n-old"],
        "account": "production:12345",
    }

    rows = list(_sync_records(client, state))

    assert client.get_filtered_sync_chunk_calls[0][0] == 0
    tombstones = [r for r in rows if r.get("_deleted")]
    assert len(tombstones) == 1
    assert tombstones[0]["id"] == "n-old"
    active = [r for r in rows if not r.get("_deleted")]
    assert len(active) == 1
    assert active[0]["id"] == "n-new"
    assert state["last_usn"] == 20
    assert state["note_ids"] == ["n-new"]


def test_only_ids_from_state_note_ids_are_tombstoned():
    """Only IDs previously ingested in state['note_ids'] are tombstoned during reconciliation."""
    note_live = SimpleNamespace(guid="n-live", title="Live", active=True)
    chunk = SyncChunk(
        chunkHighUSN=30,
        updateCount=30,
        notes=[note_live],
    )
    client = FakeResyncEvernoteClient(chunks=[chunk], update_count=30)
    # state only has "n-stale"; uningested notes missing from server are never tombstoned
    state = {
        "last_usn": 20,
        "note_ids": ["n-stale"],
        "account": "production:12345",
    }

    rows = list(_sync_records(client, state, full_resync=True))

    tombstone_ids = [r["id"] for r in rows if r.get("_deleted")]
    # Only n-stale was in state["note_ids"], so ONLY n-stale is tombstoned
    assert tombstone_ids == ["n-stale"]


def test_unknown_notebook_name_raises_before_any_tombstone():
    """Unknown notebook name raises EvernoteError before yielding tombstones.

    Leaves state untouched.
    """
    nb = SimpleNamespace(guid="nb-1", name="Engineering")
    note = SimpleNamespace(guid="n-1", title="N1", notebookGuid="nb-1", active=True)
    chunk = SyncChunk(
        chunkHighUSN=10,
        updateCount=10,
        notebooks=[nb],
        notes=[note],
    )
    client = FakeResyncEvernoteClient(chunks=[chunk], update_count=10)
    state = {
        "last_usn": 10,
        "note_ids": ["n-stale"],
        "account": "production:12345",
    }

    gen = _sync_records(client, state, notebook_names=["UnknownNB"], full_resync=True)

    with pytest.raises(EvernoteError, match="Unknown notebook name"):
        next(gen)

    # State remains untouched
    assert state["last_usn"] == 10
    assert state["note_ids"] == ["n-stale"]


def test_failure_during_pass_1_yields_nothing_and_leaves_state_unchanged():
    """An error during pass 1 chunk iteration yields nothing and leaves state unchanged."""
    client = FakeResyncEvernoteClient(update_count=10)
    client.get_filtered_sync_chunk = lambda *_: (_ for _ in ()).throw(
        RuntimeError("Network exploded during chunk fetch")
    )
    state = {
        "last_usn": 50,
        "note_ids": ["n-stale"],
        "account": "production:12345",
    }

    gen = _sync_records(client, state, full_resync=True)

    with pytest.raises(RuntimeError, match="Network exploded"):
        next(gen)

    # State untouched
    assert state["last_usn"] == 50
    assert state["note_ids"] == ["n-stale"]


def test_account_mismatch_raises_with_state_untouched():
    """Account mismatch raises EvernoteError before any chunk call, leaving state untouched."""
    client = FakeResyncEvernoteClient(user_id=999)
    # state is bound to user 123
    state = {
        "account": "production:123",
        "last_usn": 50,
        "note_ids": ["n-1"],
    }

    with pytest.raises(
        EvernoteError,
        match="this resource name is bound to a different Evernote account or environment",
    ):
        list(_sync_records(client, state))

    # No chunk call was made
    assert client.get_filtered_sync_chunk_calls == []
    # State untouched
    assert state["account"] == "production:123"
    assert state["last_usn"] == 50


def test_first_run_reconciles_nothing():
    """First run (no last_usn) reconciles nothing: zero tombstones emitted."""
    note_live = SimpleNamespace(guid="n-live", title="Live", active=True)
    chunk = SyncChunk(
        chunkHighUSN=10,
        updateCount=10,
        notes=[note_live],
    )
    client = FakeResyncEvernoteClient(chunks=[chunk], update_count=10)
    # Empty initial state (first run)
    state: dict = {}

    rows = list(_sync_records(client, state))

    tombstones = [r for r in rows if r.get("_deleted")]
    assert tombstones == []  # First run reconciles nothing

    active = [r for r in rows if not r.get("_deleted")]
    assert len(active) == 1
    assert active[0]["id"] == "n-live"

    assert state["note_ids"] == ["n-live"]
    assert state["account"] == "production:12345"


def test_maps_are_rebuilt_on_full_resync():
    """Persisted notebook/tag maps are discarded and rebuilt on full resync."""
    nb_new = SimpleNamespace(guid="nb-1", name="Engineering Reorganized")
    note = SimpleNamespace(guid="n-1", title="N1", notebookGuid="nb-1", active=True)
    chunk = SyncChunk(
        chunkHighUSN=50,
        updateCount=50,
        notebooks=[nb_new],
        notes=[note],
    )
    client = FakeResyncEvernoteClient(chunks=[chunk], update_count=50)
    state = {
        "last_usn": 40,
        "notebooks": {"nb-1": "Old Engineering Name"},
        "tags": {"t-old": "Old Tag"},
        "note_ids": ["n-1"],
        "account": "production:12345",
    }

    rows = list(_sync_records(client, state, full_resync=True))

    assert len(rows) == 1
    # Note has the new renamed notebook name
    assert rows[0]["notebook"] == "Engineering Reorganized"
    # State maps discarded old tag and updated notebook name
    assert state["notebooks"] == {"nb-1": "Engineering Reorganized"}
    assert state["tags"] == {}


def test_expunged_notebook_and_tag_dropped_from_persisted_maps():
    """Expunged notebooks and tags are dropped from persisted maps in incremental runs."""
    chunk = SyncChunk(
        chunkHighUSN=60,
        updateCount=60,
        expungedNotebooks=["nb-del"],
        expungedTags=["tag-del"],
    )
    client = FakeResyncEvernoteClient(chunks=[chunk], update_count=60)
    state = {
        "last_usn": 50,
        "notebooks": {"nb-keep": "Keep", "nb-del": "Delete"},
        "tags": {"tag-keep": "Keep", "tag-del": "Delete"},
        "note_ids": [],
        "account": "production:12345",
    }

    list(_sync_records(client, state))

    assert state["notebooks"] == {"nb-keep": "Keep"}
    assert state["tags"] == {"tag-keep": "Keep"}


def test_resync_followed_by_incremental_run_uses_new_cursor():
    """Full resync updates cursor, and a subsequent incremental run starts from that cursor."""
    nb = SimpleNamespace(guid="nb-1", name="Engineering")
    note1 = SimpleNamespace(guid="n-1", title="N1", notebookGuid="nb-1", active=True)
    chunk_resync = SyncChunk(
        chunkHighUSN=50,
        updateCount=50,
        notebooks=[nb],
        notes=[note1],
    )
    client = FakeResyncEvernoteClient(chunks=[chunk_resync], update_count=50)
    state: dict = {}

    # Run 1: Full initial sync
    list(_sync_records(client, state))
    assert state["last_usn"] == 50

    # Run 2: Incremental run
    note2 = SimpleNamespace(guid="n-2", title="N2", notebookGuid="nb-1", active=True)
    chunk_inc = SyncChunk(
        chunkHighUSN=70,
        updateCount=70,
        notes=[note2],
    )
    client.chunks = [chunk_inc]
    client.update_count = 70

    list(_sync_records(client, state))

    assert client.get_filtered_sync_chunk_calls[-1][0] == 50
    assert state["last_usn"] == 70
    assert state["note_ids"] == ["n-1", "n-2"]


def test_current_time_none_leaves_last_sync_time_unchanged():
    """When getSyncState().currentTime is None, state['last_sync_time'] is left unchanged."""
    chunk = SyncChunk(chunkHighUSN=50, updateCount=50)
    client = FakeResyncEvernoteClient(chunks=[chunk], update_count=50, current_time=None)
    state = {
        "last_usn": 40,
        "last_sync_time": 987654321,
        "account": "production:12345",
    }

    list(_sync_records(client, state))

    # last_sync_time must remain unchanged rather than being overwritten with 0 or None
    assert state["last_sync_time"] == 987654321


def test_evernote_source_accepts_full_resync_parameter():
    """evernote_source accepts full_resync parameter and sets it on the generator."""
    client = FakeResyncEvernoteClient(update_count=10)
    source = evernote_source(token="tok", client=client, full_resync=True)
    assert hasattr(source, "client")


def test_trashed_note_not_in_state_note_ids_still_yields_tombstone():
    """A trashed note that is NOT in state['note_ids'] still yields a tombstone."""
    note_trashed = SimpleNamespace(guid="n-trashed", title="Trash", active=False)
    chunk = SyncChunk(
        chunkHighUSN=10,
        updateCount=10,
        notes=[note_trashed],
    )
    client = FakeResyncEvernoteClient(chunks=[chunk], update_count=10)
    state = {
        "last_usn": 5,
        "note_ids": ["n-other"],
        "account": "production:12345",
    }

    rows = list(_sync_records(client, state))

    tombstone_ids = {r["id"] for r in rows if r.get("_deleted")}
    assert "n-trashed" in tombstone_ids
