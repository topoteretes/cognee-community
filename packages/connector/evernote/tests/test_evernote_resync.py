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
- scope change (notebook or tag widen/narrow/None transition) triggers a full resync
- scope unchanged stays incremental
- failure during pass 1 on scope change leaves state and state["scope"] unchanged
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
        "v": 1,
        "last_usn": 50,
        "last_sync_time": 1000,
        "note_ids": {"n-live": "nb-1", "n-stale": "nb-1"},
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
    assert state["note_ids"] == {"n-live": "nb-1"}


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
        "v": 1,
        "last_usn": 50,
        "last_sync_time": 1000,
        "note_ids": {"n-live": "nb-1", "n-stale": "nb-1"},
        "account": "production:12345",
    }

    # Pass full_resync=True
    rows = list(_sync_records(client, state, full_resync=True))

    assert client.get_filtered_sync_chunk_calls[0][0] == 0
    tombstones = [r for r in rows if r.get("_deleted")]
    assert len(tombstones) == 1
    assert tombstones[0]["id"] == "n-stale"
    assert state["note_ids"] == {"n-live": "nb-1"}


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
        "v": 1,
        "last_usn": 50,
        "note_ids": {"n-old": "nb-1"},
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
    assert state["note_ids"] == {"n-new": ""}


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
        "v": 1,
        "last_usn": 20,
        "note_ids": {"n-stale": "nb-1"},
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
        "v": 1,
        "last_usn": 10,
        "note_ids": {"n-stale": "nb-1"},
        "account": "production:12345",
    }

    gen = _sync_records(client, state, notebook_names=["UnknownNB"], full_resync=True)

    with pytest.raises(
        EvernoteError,
        match=(
            r"Notebook name\(s\) not found: .*\. If a notebook was renamed, "
            r"update notebook_names\."
        ),
    ):
        next(gen)

    # State remains untouched
    assert state["last_usn"] == 10
    assert state["note_ids"] == {"n-stale": "nb-1"}


def test_failure_during_pass_1_yields_nothing_and_leaves_state_unchanged():
    """An error during pass 1 chunk iteration yields nothing and leaves state unchanged."""
    client = FakeResyncEvernoteClient(update_count=10)
    client.get_filtered_sync_chunk = lambda *_: (_ for _ in ()).throw(
        RuntimeError("Network exploded during chunk fetch")
    )
    state = {
        "v": 1,
        "last_usn": 50,
        "note_ids": {"n-stale": "nb-1"},
        "account": "production:12345",
    }

    gen = _sync_records(client, state, full_resync=True)

    with pytest.raises(RuntimeError, match="Network exploded"):
        next(gen)

    # State untouched
    assert state["last_usn"] == 50
    assert state["note_ids"] == {"n-stale": "nb-1"}


def test_account_mismatch_raises_with_state_untouched():
    """Account mismatch raises EvernoteError before any chunk call, leaving state untouched."""
    client = FakeResyncEvernoteClient(user_id=999)
    # state is bound to user 123
    state = {
        "v": 1,
        "account": "production:123",
        "last_usn": 50,
        "note_ids": {"n-1": "nb-1"},
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

    assert state["note_ids"] == {"n-live": ""}
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
        "v": 1,
        "last_usn": 40,
        "notebooks": {"nb-1": "Old Engineering Name"},
        "tags": {"t-old": "Old Tag"},
        "note_ids": {"n-1": "nb-1"},
        "account": "production:12345",
    }

    rows = list(_sync_records(client, state, full_resync=True))

    assert len(rows) == 1
    # Note has the new renamed notebook name in content header
    assert "Notebook: Engineering Reorganized" in rows[0]["content"]
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
        "v": 1,
        "last_usn": 50,
        "notebooks": {"nb-keep": "Keep", "nb-del": "Delete"},
        "tags": {"tag-keep": "Keep", "tag-del": "Delete"},
        "note_ids": {},
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
    assert state["note_ids"] == {"n-1": "nb-1", "n-2": "nb-1"}


def test_current_time_none_leaves_last_sync_time_unchanged():
    """When getSyncState().currentTime is None, state['last_sync_time'] is left unchanged."""
    chunk = SyncChunk(chunkHighUSN=50, updateCount=50)
    client = FakeResyncEvernoteClient(chunks=[chunk], update_count=50, current_time=None)
    state = {
        "v": 1,
        "last_usn": 40,
        "last_sync_time": 987654321,
        "account": "production:12345",
        "note_ids": {},
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
        "v": 1,
        "last_usn": 5,
        "note_ids": {"n-other": "nb-1"},
        "account": "production:12345",
    }

    rows = list(_sync_records(client, state))

    tombstone_ids = {r["id"] for r in rows if r.get("_deleted")}
    assert "n-trashed" in tombstone_ids


def test_scope_widen_triggers_full_resync():
    """Widening notebook scope triggers full resync from USN 0 even if updateCount unchanged."""
    nb_a = SimpleNamespace(guid="nb-a", name="NotebookA")
    nb_b = SimpleNamespace(guid="nb-b", name="NotebookB")
    note_a = SimpleNamespace(guid="note-a", title="A", notebookGuid="nb-a", active=True)
    note_b = SimpleNamespace(guid="note-b", title="B", notebookGuid="nb-b", active=True)

    # Run 1: scoped to NotebookA
    chunk1 = SyncChunk(
        chunkHighUSN=10,
        updateCount=10,
        notebooks=[nb_a, nb_b],
        notes=[note_a],
    )
    client1 = FakeResyncEvernoteClient(chunks=[chunk1], update_count=10)
    state: dict = {}
    rows1 = list(_sync_records(client1, state, notebook_names=["NotebookA"]))

    assert len(rows1) == 1
    assert rows1[0]["id"] == "note-a"
    assert state["last_usn"] == 10
    assert state["scope"] == {
        "notebooks": ["NotebookA"],
        "tags": None,
        "notebook_guids": ["nb-a"],
        "tag_guids": None,
    }
    assert state["note_ids"] == {"note-a": "nb-a"}

    # Run 2: widen scope to NotebookA and NotebookB, server updateCount is still 10!
    chunk2 = SyncChunk(
        chunkHighUSN=10,
        updateCount=10,
        notebooks=[nb_a, nb_b],
        notes=[note_a, note_b],
    )
    client2 = FakeResyncEvernoteClient(chunks=[chunk2], update_count=10)
    rows2 = list(_sync_records(client2, state, notebook_names=["NotebookA", "NotebookB"]))

    # Assert read started from USN 0 because scope changed
    assert client2.get_filtered_sync_chunk_calls[0][0] == 0
    active_ids = {r["id"] for r in rows2 if not r.get("_deleted")}
    assert active_ids == {"note-a", "note-b"}
    assert state["scope"] == {
        "notebooks": ["NotebookA", "NotebookB"],
        "tags": None,
        "notebook_guids": ["nb-a", "nb-b"],
        "tag_guids": None,
    }
    assert state["note_ids"] == {"note-a": "nb-a", "note-b": "nb-b"}


def test_scope_narrow_tombstones_out_of_scope_notes():
    """Narrowing notebook scope triggers a full resync and tombstones notes no longer in scope."""
    nb_a = SimpleNamespace(guid="nb-a", name="NotebookA")
    nb_b = SimpleNamespace(guid="nb-b", name="NotebookB")
    note_a = SimpleNamespace(guid="note-a", title="A", notebookGuid="nb-a", active=True)
    note_b = SimpleNamespace(guid="note-b", title="B", notebookGuid="nb-b", active=True)

    # Run 1: scoped to both
    chunk1 = SyncChunk(
        chunkHighUSN=10,
        updateCount=10,
        notebooks=[nb_a, nb_b],
        notes=[note_a, note_b],
    )
    client1 = FakeResyncEvernoteClient(chunks=[chunk1], update_count=10)
    state: dict = {}
    list(_sync_records(client1, state, notebook_names=["NotebookA", "NotebookB"]))
    assert state["note_ids"] == {"note-a": "nb-a", "note-b": "nb-b"}

    # Run 2: narrow to NotebookA only, server updateCount unchanged
    chunk2 = SyncChunk(
        chunkHighUSN=10,
        updateCount=10,
        notebooks=[nb_a, nb_b],
        notes=[note_a, note_b],
    )
    client2 = FakeResyncEvernoteClient(chunks=[chunk2], update_count=10)
    rows2 = list(_sync_records(client2, state, notebook_names=["NotebookA"]))

    assert client2.get_filtered_sync_chunk_calls[0][0] == 0
    active_ids = {r["id"] for r in rows2 if not r.get("_deleted")}
    tombstone_ids = {r["id"] for r in rows2 if r.get("_deleted")}

    assert active_ids == {"note-a"}
    assert "note-b" in tombstone_ids
    assert state["note_ids"] == {"note-a": "nb-a"}
    assert state["scope"] == {
        "notebooks": ["NotebookA"],
        "tags": None,
        "notebook_guids": ["nb-a"],
        "tag_guids": None,
    }


def test_scope_narrow_listing_failure_leaves_state_unchanged():
    """Listing error on scope change yields nothing and leaves state and scope unchanged."""
    nb_a = SimpleNamespace(guid="nb-a", name="NotebookA")
    nb_b = SimpleNamespace(guid="nb-b", name="NotebookB")
    note_a = SimpleNamespace(guid="note-a", title="A", notebookGuid="nb-a", active=True)
    note_b = SimpleNamespace(guid="note-b", title="B", notebookGuid="nb-b", active=True)

    chunk1 = SyncChunk(
        chunkHighUSN=10,
        updateCount=10,
        notebooks=[nb_a, nb_b],
        notes=[note_a, note_b],
    )
    client1 = FakeResyncEvernoteClient(chunks=[chunk1], update_count=10)
    state: dict = {}
    list(_sync_records(client1, state, notebook_names=["NotebookA", "NotebookB"]))

    initial_scope = dict(state["scope"])
    initial_note_ids = dict(state["note_ids"])
    initial_usn = state["last_usn"]

    client2 = FakeResyncEvernoteClient(update_count=10)
    client2.get_filtered_sync_chunk = lambda *_: (_ for _ in ()).throw(
        RuntimeError("Chunk fetch failed")
    )

    gen = _sync_records(client2, state, notebook_names=["NotebookA"])
    with pytest.raises(RuntimeError, match="Chunk fetch failed"):
        next(gen)

    # State including state["scope"] and state["note_ids"] must be untouched
    assert state["scope"] == initial_scope
    assert state["note_ids"] == initial_note_ids
    assert state["last_usn"] == initial_usn


def test_scope_unchanged_stays_incremental():
    """When scope is unchanged, sync remains incremental (uses afterUSN = last_usn or shortcuts)."""
    nb_a = SimpleNamespace(guid="nb-a", name="NotebookA")
    note_a1 = SimpleNamespace(guid="note-a1", title="A1", notebookGuid="nb-a", active=True)

    # Run 1
    chunk1 = SyncChunk(
        chunkHighUSN=10,
        updateCount=10,
        notebooks=[nb_a],
        notes=[note_a1],
    )
    client1 = FakeResyncEvernoteClient(chunks=[chunk1], update_count=10)
    state: dict = {}
    list(_sync_records(client1, state, notebook_names=["NotebookA"]))
    assert state["last_usn"] == 10

    # Run 2: same scope, updateCount == last_usn -> shortcut triggers, no chunk calls
    client2 = FakeResyncEvernoteClient(update_count=10)
    rows2 = list(_sync_records(client2, state, notebook_names=["NotebookA"]))
    assert client2.get_filtered_sync_chunk_calls == []
    # Materialize table schema marker yielded
    assert len(rows2) == 1

    # Run 3: same scope, updateCount > last_usn -> incremental sync from afterUSN = 10
    note_a2 = SimpleNamespace(guid="note-a2", title="A2", notebookGuid="nb-a", active=True)
    chunk3 = SyncChunk(
        chunkHighUSN=15,
        updateCount=15,
        notebooks=[nb_a],
        notes=[note_a2],
    )
    client3 = FakeResyncEvernoteClient(chunks=[chunk3], update_count=15)
    rows3 = list(_sync_records(client3, state, notebook_names=["NotebookA"]))
    assert client3.get_filtered_sync_chunk_calls[0][0] == 10
    active_ids = {r["id"] for r in rows3 if not r.get("_deleted")}
    assert active_ids == {"note-a2"}
    assert state["last_usn"] == 15


def test_tag_scope_change_triggers_full_resync():
    """Changing tag scope triggers a full resync from USN 0 even if updateCount is unchanged."""
    tag1 = SimpleNamespace(guid="t1", name="Tag1")
    tag2 = SimpleNamespace(guid="t2", name="Tag2")
    note1 = SimpleNamespace(guid="n1", title="N1", tagGuids=["t1"], active=True)
    note2 = SimpleNamespace(guid="n2", title="N2", tagGuids=["t2"], active=True)

    # Run 1: tag_names=["Tag1"]
    chunk1 = SyncChunk(
        chunkHighUSN=10,
        updateCount=10,
        tags=[tag1, tag2],
        notes=[note1],
    )
    client1 = FakeResyncEvernoteClient(chunks=[chunk1], update_count=10)
    state: dict = {}
    rows1 = list(_sync_records(client1, state, tag_names=["Tag1"]))
    assert len(rows1) == 1
    assert rows1[0]["id"] == "n1"
    assert state["scope"] == {
        "notebooks": None,
        "tags": ["Tag1"],
        "notebook_guids": None,
        "tag_guids": ["t1"],
    }

    # Run 2: tag_names=["Tag1", "Tag2"], server updateCount unchanged
    chunk2 = SyncChunk(
        chunkHighUSN=10,
        updateCount=10,
        tags=[tag1, tag2],
        notes=[note1, note2],
    )
    client2 = FakeResyncEvernoteClient(chunks=[chunk2], update_count=10)
    rows2 = list(_sync_records(client2, state, tag_names=["Tag1", "Tag2"]))
    assert client2.get_filtered_sync_chunk_calls[0][0] == 0
    active_ids = {r["id"] for r in rows2 if not r.get("_deleted")}
    assert active_ids == {"n1", "n2"}
    assert state["scope"] == {
        "notebooks": None,
        "tags": ["Tag1", "Tag2"],
        "notebook_guids": None,
        "tag_guids": ["t1", "t2"],
    }


def test_scope_none_to_list_transition():
    """Transitioning from no scope filter (None) to a list triggers a full resync."""
    nb = SimpleNamespace(guid="nb1", name="Engineering")
    note = SimpleNamespace(guid="n1", title="N1", notebookGuid="nb1", active=True)

    chunk1 = SyncChunk(chunkHighUSN=10, updateCount=10, notebooks=[nb], notes=[note])
    client1 = FakeResyncEvernoteClient(chunks=[chunk1], update_count=10)
    state: dict = {}
    list(_sync_records(client1, state))
    assert state["scope"] == {
        "notebooks": None,
        "tags": None,
        "notebook_guids": None,
        "tag_guids": None,
    }

    # Run 2: scope added
    chunk2 = SyncChunk(chunkHighUSN=10, updateCount=10, notebooks=[nb], notes=[note])
    client2 = FakeResyncEvernoteClient(chunks=[chunk2], update_count=10)
    list(_sync_records(client2, state, notebook_names=["Engineering"]))
    assert client2.get_filtered_sync_chunk_calls[0][0] == 0
    assert state["scope"] == {
        "notebooks": ["Engineering"],
        "tags": None,
        "notebook_guids": ["nb1"],
        "tag_guids": None,
    }


def test_scope_list_to_none_transition():
    """Transitioning from a list filter to no filter (None) triggers a full resync."""
    nb = SimpleNamespace(guid="nb1", name="Engineering")
    note = SimpleNamespace(guid="n1", title="N1", notebookGuid="nb1", active=True)

    chunk1 = SyncChunk(chunkHighUSN=10, updateCount=10, notebooks=[nb], notes=[note])
    client1 = FakeResyncEvernoteClient(chunks=[chunk1], update_count=10)
    state: dict = {}
    list(_sync_records(client1, state, notebook_names=["Engineering"]))
    assert state["scope"] == {
        "notebooks": ["Engineering"],
        "tags": None,
        "notebook_guids": ["nb1"],
        "tag_guids": None,
    }

    # Run 2: scope removed (None)
    chunk2 = SyncChunk(chunkHighUSN=10, updateCount=10, notebooks=[nb], notes=[note])
    client2 = FakeResyncEvernoteClient(chunks=[chunk2], update_count=10)
    list(_sync_records(client2, state))
    assert client2.get_filtered_sync_chunk_calls[0][0] == 0
    assert state["scope"] == {
        "notebooks": None,
        "tags": None,
        "notebook_guids": None,
        "tag_guids": None,
    }


def test_notebook_scope_guid_swap_triggers_full_resync_and_tombstones_old():
    """Notebook name swap (different GUID takes same name) triggers full resync."""
    nb_w1 = SimpleNamespace(guid="W1", name="Work")
    note_n1 = SimpleNamespace(guid="n1", title="Note 1", notebookGuid="W1", active=True)

    # Run 1: "Work" is W1 with note n1
    chunk1 = SyncChunk(chunkHighUSN=10, updateCount=10, notebooks=[nb_w1], notes=[note_n1])
    client = FakeResyncEvernoteClient(chunks=[chunk1], update_count=10)
    state: dict = {}
    rows1 = list(_sync_records(client, state, notebook_names=["Work"]))
    assert len(rows1) == 1
    assert rows1[0]["id"] == "n1"
    assert state["note_ids"] == {"n1": "W1"}
    assert state["scope"]["notebook_guids"] == ["W1"]

    # Before Run 2: W1 is renamed "Old", and new notebook W2 is named "Work" with note n2.
    # Note n1 is not in the chunk (it was not modified).
    nb_w1_renamed = SimpleNamespace(guid="W1", name="Old")
    nb_w2_new = SimpleNamespace(guid="W2", name="Work")
    note_n2 = SimpleNamespace(guid="n2", title="Note 2", notebookGuid="W2", active=True)

    chunk2_incremental = SyncChunk(
        chunkHighUSN=20,
        updateCount=20,
        notebooks=[nb_w1_renamed, nb_w2_new],
        notes=[note_n2],
    )
    # Full resync chunk from USN 0 (called when guid scope change detected)
    chunk2_full = SyncChunk(
        chunkHighUSN=20,
        updateCount=20,
        notebooks=[nb_w1_renamed, nb_w2_new],
        notes=[note_n1, note_n2],
    )
    client.chunks = [chunk2_incremental, chunk2_full]
    client.update_count = 20

    rows2 = list(_sync_records(client, state, notebook_names=["Work"]))

    # n1 was tombstoned (no longer in scope W2), n2 loaded
    tombstones = [r for r in rows2 if r.get("_deleted")]
    active = [r for r in rows2 if not r.get("_deleted")]
    assert len(tombstones) == 1
    assert tombstones[0]["id"] == "n1"
    assert len(active) == 1
    assert active[0]["id"] == "n2"

    assert state["note_ids"] == {"n2": "W2"}
    assert state["scope"]["notebook_guids"] == ["W2"]


def test_notebook_rename_only_raises_and_leaves_state_unchanged():
    """When a scoped notebook is renamed and not found, raise and leave state untouched."""
    nb_w1 = SimpleNamespace(guid="W1", name="Work")
    note_n1 = SimpleNamespace(guid="n1", title="Note 1", notebookGuid="W1", active=True)

    chunk1 = SyncChunk(chunkHighUSN=10, updateCount=10, notebooks=[nb_w1], notes=[note_n1])
    client = FakeResyncEvernoteClient(chunks=[chunk1], update_count=10)
    state: dict = {}
    list(_sync_records(client, state, notebook_names=["Work"]))
    state_after_run1 = dict(state)

    # W1 is renamed "Old", no notebook named "Work" exists
    nb_w1_renamed = SimpleNamespace(guid="W1", name="Old")
    chunk2 = SyncChunk(chunkHighUSN=20, updateCount=20, notebooks=[nb_w1_renamed], notes=[])
    client.chunks = [chunk2]
    client.update_count = 20

    gen = _sync_records(client, state, notebook_names=["Work"])
    with pytest.raises(
        EvernoteError,
        match=(
            r"Notebook name\(s\) not found: .*\. If a notebook was renamed, "
            r"update notebook_names\."
        ),
    ):
        next(gen)

    # State must be completely untouched
    assert state == state_after_run1


def test_tag_scope_guid_swap_triggers_full_resync_and_tombstones_old():
    """Tag name swap (different GUID takes same tag name) triggers full resync."""
    tag_t1 = SimpleNamespace(guid="T1", name="Urgent")
    note_n1 = SimpleNamespace(guid="n1", title="N1", tagGuids=["T1"], active=True)

    chunk1 = SyncChunk(chunkHighUSN=10, updateCount=10, tags=[tag_t1], notes=[note_n1])
    client = FakeResyncEvernoteClient(chunks=[chunk1], update_count=10)
    state: dict = {}
    rows1 = list(_sync_records(client, state, tag_names=["Urgent"]))
    assert len(rows1) == 1
    assert state["scope"]["tag_guids"] == ["T1"]

    # Before Run 2: T1 is renamed "OldUrgent", T2 is named "Urgent" with note n2.
    tag_t1_renamed = SimpleNamespace(guid="T1", name="OldUrgent")
    tag_t2_new = SimpleNamespace(guid="T2", name="Urgent")
    note_n2 = SimpleNamespace(guid="n2", title="N2", tagGuids=["T2"], active=True)

    chunk2_incremental = SyncChunk(
        chunkHighUSN=20,
        updateCount=20,
        tags=[tag_t1_renamed, tag_t2_new],
        notes=[note_n2],
    )
    chunk2_full = SyncChunk(
        chunkHighUSN=20,
        updateCount=20,
        tags=[tag_t1_renamed, tag_t2_new],
        notes=[note_n1, note_n2],
    )
    client.chunks = [chunk2_incremental, chunk2_full]
    client.update_count = 20

    rows2 = list(_sync_records(client, state, tag_names=["Urgent"]))

    tombstones = [r for r in rows2 if r.get("_deleted")]
    active = [r for r in rows2 if not r.get("_deleted")]
    assert len(tombstones) == 1
    assert tombstones[0]["id"] == "n1"
    assert len(active) == 1
    assert active[0]["id"] == "n2"
    assert state["scope"]["tag_guids"] == ["T2"]


def test_tag_rename_only_raises_and_leaves_state_unchanged():
    """When a scoped tag is renamed and not found, raise and leave state untouched."""
    tag_t1 = SimpleNamespace(guid="T1", name="Urgent")
    note_n1 = SimpleNamespace(guid="n1", title="N1", tagGuids=["T1"], active=True)

    chunk1 = SyncChunk(chunkHighUSN=10, updateCount=10, tags=[tag_t1], notes=[note_n1])
    client = FakeResyncEvernoteClient(chunks=[chunk1], update_count=10)
    state: dict = {}
    list(_sync_records(client, state, tag_names=["Urgent"]))
    state_after_run1 = dict(state)

    tag_t1_renamed = SimpleNamespace(guid="T1", name="OldUrgent")
    chunk2 = SyncChunk(chunkHighUSN=20, updateCount=20, tags=[tag_t1_renamed], notes=[])
    client.chunks = [chunk2]
    client.update_count = 20

    gen = _sync_records(client, state, tag_names=["Urgent"])
    with pytest.raises(
        EvernoteError,
        match=r"Tag name\(s\) not found: .*\. If a tag was renamed, update tag_names\.",
    ):
        next(gen)

    assert state == state_after_run1


def test_configured_notebook_scope_missing_guids_triggers_full_resync_when_server_unchanged():
    """Notebook scope missing persisted GUIDs triggers full resync even if server unchanged."""
    nb1 = SimpleNamespace(guid="nb-1", name="Engineering")
    note_n1 = SimpleNamespace(guid="n-1", title="Eng Note", notebookGuid="nb-1", active=True)
    chunk = SyncChunk(chunkHighUSN=50, updateCount=50, notebooks=[nb1], notes=[note_n1])
    client = FakeResyncEvernoteClient(chunks=[chunk], update_count=50)

    # v1 state with matching scope names but missing notebook_guids
    state = {
        "v": 1,
        "account": "production:12345",
        "last_usn": 50,
        "last_sync_time": 1609459200000,
        "notebooks": {"nb-1": "Engineering"},
        "tags": {},
        "note_ids": {"n-1": "nb-1"},
        "scope": {
            "notebooks": ["Engineering"],
            "tags": None,
            "notebook_guids": None,
            "tag_guids": None,
        },
    }

    rows = list(_sync_records(client, state, notebook_names=["Engineering"]))

    # Must start full resync from USN 0, bypassing server updateCount == last_usn shortcut
    assert len(client.get_filtered_sync_chunk_calls) == 1
    assert client.get_filtered_sync_chunk_calls[0][0] == 0
    assert len(rows) == 1
    assert rows[0]["id"] == "n-1"
    assert state["scope"]["notebook_guids"] == ["nb-1"]


def test_configured_tag_scope_missing_guids_triggers_full_resync_when_server_unchanged():
    """Tag scope missing persisted GUIDs triggers full resync even if server unchanged."""
    tag1 = SimpleNamespace(guid="t-1", name="Urgent")
    note_n1 = SimpleNamespace(
        guid="n-1", title="Urgent Note", notebookGuid="nb-1", tagGuids=["t-1"], active=True
    )
    chunk = SyncChunk(chunkHighUSN=50, updateCount=50, tags=[tag1], notes=[note_n1])
    client = FakeResyncEvernoteClient(chunks=[chunk], update_count=50)

    # v1 state with matching scope names but missing tag_guids
    state = {
        "v": 1,
        "account": "production:12345",
        "last_usn": 50,
        "last_sync_time": 1609459200000,
        "notebooks": {},
        "tags": {"t-1": "Urgent"},
        "note_ids": {"n-1": "nb-1"},
        "scope": {
            "notebooks": None,
            "tags": ["Urgent"],
            "notebook_guids": None,
            "tag_guids": None,
        },
    }

    rows = list(_sync_records(client, state, tag_names=["Urgent"]))

    # Must start full resync from USN 0, bypassing server updateCount == last_usn shortcut
    assert len(client.get_filtered_sync_chunk_calls) == 1
    assert client.get_filtered_sync_chunk_calls[0][0] == 0
    assert len(rows) == 1
    assert rows[0]["id"] == "n-1"
    assert state["scope"]["tag_guids"] == ["t-1"]


def test_configured_scope_with_valid_guids_preserves_no_change_optimization():
    """Configured scope with valid matching GUIDs preserves server no-change shortcut."""
    client = FakeResyncEvernoteClient(update_count=50, current_time=1609500000000)

    state = {
        "v": 1,
        "account": "production:12345",
        "last_usn": 50,
        "last_sync_time": 1609459200000,
        "notebooks": {"nb-1": "Engineering"},
        "tags": {"t-1": "Urgent"},
        "note_ids": {"n-1": "nb-1"},
        "scope": {
            "notebooks": ["Engineering"],
            "tags": ["Urgent"],
            "notebook_guids": ["nb-1"],
            "tag_guids": ["t-1"],
        },
    }

    rows = list(
        _sync_records(
            client,
            state,
            notebook_names=["Engineering"],
            tag_names=["Urgent"],
        )
    )

    data_rows = [r for r in rows if isinstance(r, dict)]
    assert data_rows == []
    assert client.get_filtered_sync_chunk_calls == []
    assert state["last_sync_time"] == 1609459200000


def test_configured_notebook_scope_empty_guids_triggers_full_resync():
    """Notebook scope with empty persisted GUID list triggers full resync."""
    nb1 = SimpleNamespace(guid="nb-1", name="Engineering")
    note_n1 = SimpleNamespace(guid="n-1", title="Eng Note", notebookGuid="nb-1", active=True)
    chunk = SyncChunk(chunkHighUSN=50, updateCount=50, notebooks=[nb1], notes=[note_n1])
    client = FakeResyncEvernoteClient(chunks=[chunk], update_count=50)

    # v1 state with matching scope names but empty notebook_guids list
    state = {
        "v": 1,
        "account": "production:12345",
        "last_usn": 50,
        "last_sync_time": 1609459200000,
        "notebooks": {"nb-1": "Engineering"},
        "tags": {},
        "note_ids": {"n-1": "nb-1"},
        "scope": {
            "notebooks": ["Engineering"],
            "tags": None,
            "notebook_guids": [],
            "tag_guids": None,
        },
    }

    rows = list(_sync_records(client, state, notebook_names=["Engineering"]))

    # Must bypass the no-change shortcut and start from USN 0
    assert len(client.get_filtered_sync_chunk_calls) >= 1
    assert client.get_filtered_sync_chunk_calls[0][0] == 0
    assert any(r["id"] == "n-1" for r in rows if isinstance(r, dict))
    assert state["scope"]["notebook_guids"] == ["nb-1"]


def test_configured_tag_scope_empty_guids_triggers_full_resync():
    """Tag scope with empty persisted GUID list triggers full resync."""
    tag1 = SimpleNamespace(guid="t-1", name="Urgent")
    note_n1 = SimpleNamespace(
        guid="n-1",
        title="Urgent Note",
        notebookGuid="nb-1",
        tagGuids=["t-1"],
        active=True,
    )
    chunk = SyncChunk(chunkHighUSN=50, updateCount=50, tags=[tag1], notes=[note_n1])
    client = FakeResyncEvernoteClient(chunks=[chunk], update_count=50)

    # v1 state with matching scope names but empty tag_guids list
    state = {
        "v": 1,
        "account": "production:12345",
        "last_usn": 50,
        "last_sync_time": 1609459200000,
        "notebooks": {},
        "tags": {"t-1": "Urgent"},
        "note_ids": {"n-1": "nb-1"},
        "scope": {
            "notebooks": None,
            "tags": ["Urgent"],
            "notebook_guids": None,
            "tag_guids": [],
        },
    }

    rows = list(_sync_records(client, state, tag_names=["Urgent"]))

    # Must bypass the no-change shortcut and start from USN 0
    assert len(client.get_filtered_sync_chunk_calls) >= 1
    assert client.get_filtered_sync_chunk_calls[0][0] == 0
    assert any(r["id"] == "n-1" for r in rows if isinstance(r, dict))
    assert state["scope"]["tag_guids"] == ["t-1"]


def test_expunged_notebook_tombstones_its_notes_and_removes_from_note_ids():
    """Expunged notebook yields tombstones for all its ingested notes and cleans up note_ids."""
    nb_a = SimpleNamespace(guid="nb-a", name="NotebookA")
    nb_b = SimpleNamespace(guid="nb-b", name="NotebookB")
    note_a1 = SimpleNamespace(guid="a1", title="A1", notebookGuid="nb-a", active=True)
    note_a2 = SimpleNamespace(guid="a2", title="A2", notebookGuid="nb-a", active=True)
    note_b1 = SimpleNamespace(guid="b1", title="B1", notebookGuid="nb-b", active=True)

    # Run 1: ingest both notebooks
    chunk1 = SyncChunk(
        chunkHighUSN=10,
        updateCount=10,
        notebooks=[nb_a, nb_b],
        notes=[note_a1, note_a2, note_b1],
    )
    client = FakeResyncEvernoteClient(chunks=[chunk1], update_count=10)
    state: dict = {}
    rows1 = list(_sync_records(client, state))
    assert len(rows1) == 3
    assert state["note_ids"] == {"a1": "nb-a", "a2": "nb-a", "b1": "nb-b"}

    # Run 2: chunk expunges NotebookA without listing a1 or a2 in expungedNotes
    chunk2 = SyncChunk(
        chunkHighUSN=20,
        updateCount=20,
        expungedNotebooks=["nb-a"],
    )
    client.chunks = [chunk2]
    client.update_count = 20

    rows2 = list(_sync_records(client, state))

    tombstones = [r for r in rows2 if r.get("_deleted")]
    tombstone_ids = {r["id"] for r in tombstones}
    assert tombstone_ids == {"a1", "a2"}
    assert state["note_ids"] == {"b1": "nb-b"}
    assert "nb-a" not in state["notebooks"]


def test_note_moved_out_before_notebook_expunged_is_not_tombstoned():
    """If a note moved out of a notebook before the notebook was expunged, it is not tombstoned."""
    nb_a = SimpleNamespace(guid="nb-a", name="NotebookA")
    nb_b = SimpleNamespace(guid="nb-b", name="NotebookB")
    note_a1 = SimpleNamespace(guid="a1", title="A1", notebookGuid="nb-a", active=True)
    note_b1 = SimpleNamespace(guid="b1", title="B1", notebookGuid="nb-b", active=True)

    chunk1 = SyncChunk(
        chunkHighUSN=10,
        updateCount=10,
        notebooks=[nb_a, nb_b],
        notes=[note_a1, note_b1],
    )
    client = FakeResyncEvernoteClient(chunks=[chunk1], update_count=10)
    state: dict = {}
    list(_sync_records(client, state))

    # Run 2: a1 moved to nb-b, AND nb-a is expunged in the same chunk
    note_a1_moved = SimpleNamespace(guid="a1", title="A1", notebookGuid="nb-b", active=True)
    chunk2 = SyncChunk(
        chunkHighUSN=20,
        updateCount=20,
        notes=[note_a1_moved],
        expungedNotebooks=["nb-a"],
    )
    client.chunks = [chunk2]
    client.update_count = 20

    rows2 = list(_sync_records(client, state))

    tombstones = [r for r in rows2 if r.get("_deleted")]
    active = [r for r in rows2 if not r.get("_deleted")]
    # No tombstones: a1 was moved to nb-b, not deleted
    assert tombstones == []
    assert len(active) == 1
    assert active[0]["id"] == "a1"
    assert state["note_ids"] == {"a1": "nb-b", "b1": "nb-b"}


def test_expunged_notebook_with_note_also_in_expunged_notes_emits_one_tombstone():
    """When a note is in expungedNotebooks AND expungedNotes, only one tombstone is emitted."""
    nb_a = SimpleNamespace(guid="nb-a", name="NotebookA")
    note_a1 = SimpleNamespace(guid="a1", title="A1", notebookGuid="nb-a", active=True)

    chunk1 = SyncChunk(chunkHighUSN=10, updateCount=10, notebooks=[nb_a], notes=[note_a1])
    client = FakeResyncEvernoteClient(chunks=[chunk1], update_count=10)
    state: dict = {}
    list(_sync_records(client, state))

    # Run 2: nb-a in expungedNotebooks AND a1 in expungedNotes
    chunk2 = SyncChunk(
        chunkHighUSN=20,
        updateCount=20,
        expungedNotebooks=["nb-a"],
        expungedNotes=["a1"],
    )
    client.chunks = [chunk2]
    client.update_count = 20

    rows2 = list(_sync_records(client, state))

    tombstones = [r for r in rows2 if r.get("_deleted")]
    assert len(tombstones) == 1
    assert tombstones[0]["id"] == "a1"
    assert state["note_ids"] == {}
