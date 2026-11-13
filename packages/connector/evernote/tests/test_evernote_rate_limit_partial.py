"""Unit tests for rate limit handling, batch bounding, and partial sync recovery.

Tests verify:
1. Reviewer scenario: 10 notes, rate limit on 7th body fetch -> 6 rows yielded,
   partial recorded, run 2 fetches only remaining 4, cursor advances, partial removed.
2. max_notes_per_run=4 over 10 notes finishes in 3 runs (4+4+2 fetches).
3. An edited note between runs is fetched again; untouched earlier notes are not.
4. Tombstones are emitted during an unfinished pass.
5. Rate limit on very first body fetch ends cleanly with warning and no data rows.
6. Rate limit during pass 1 ends cleanly with no state mutation and warning logged.
7. Interrupted scope-change resync continues on matching scope; restarts on changed scope.
8. Configured empty scope lists (notebook_names=[], tag_names=[]) normalize to None
   and second run is a no-op.
"""

from types import SimpleNamespace
from typing import Any

from evernote.edam.notestore.ttypes import SyncChunk, SyncState

from cognee_community_connector_evernote import EvernoteRateLimitError
from cognee_community_connector_evernote.evernote import _sync_records


class FakeRateLimitEvernoteClient:
    def __init__(
        self,
        chunks: list[Any] | None = None,
        note_contents: dict[str, Any] | None = None,
        update_count: int = 100,
        current_time: int = 1609459200000,
        full_sync_before: int = 0,
        user_id: int = 12345,
        rate_limit_on_fetch_call: int | None = None,
        rate_limit_in_pass_1: bool = False,
        rate_limit_in_sync_state: bool = False,
    ) -> None:
        self.chunks = list(chunks or [])
        self.note_contents = dict(note_contents or {})
        self.update_count = update_count
        self.current_time = current_time
        self.full_sync_before = full_sync_before
        self.user_id = user_id
        self.rate_limit_on_fetch_call = rate_limit_on_fetch_call
        self.rate_limit_in_pass_1 = rate_limit_in_pass_1
        self.rate_limit_in_sync_state = rate_limit_in_sync_state
        self.get_filtered_sync_chunk_calls: list[tuple[int, int, Any]] = []
        self.get_note_content_calls: list[str] = []
        self.get_sync_state_calls = 0

    def get_user_id(self) -> int:
        return self.user_id

    def get_sync_state(self) -> SyncState:
        self.get_sync_state_calls += 1
        if self.rate_limit_in_sync_state:
            raise EvernoteRateLimitError("Rate limit in sync state", retry_after=60)
        return SyncState(
            currentTime=self.current_time,
            fullSyncBefore=self.full_sync_before,
            updateCount=self.update_count,
        )

    def get_filtered_sync_chunk(
        self, after_usn: int, max_entries: int, filter_obj: Any
    ) -> SyncChunk:
        self.get_filtered_sync_chunk_calls.append((after_usn, max_entries, filter_obj))
        if self.rate_limit_in_pass_1:
            raise EvernoteRateLimitError("Rate limit in pass 1", retry_after=45)
        if self.chunks:
            return self.chunks.pop(0)
        return SyncChunk(chunkHighUSN=None)

    def get_note_content(self, guid: str) -> str:
        self.get_note_content_calls.append(guid)
        if (
            self.rate_limit_on_fetch_call is not None
            and len(self.get_note_content_calls) == self.rate_limit_on_fetch_call
        ):
            raise EvernoteRateLimitError("Evernote rate limit reached", retry_after=30)
        if guid in self.note_contents:
            val = self.note_contents[guid]
            if isinstance(val, Exception):
                raise val
            return val
        return f"<en-note><div>Body for {guid}</div></en-note>"


def test_reviewer_scenario_rate_limit_on_seventh_fetch():
    """10 notes, rate limit on 7th body fetch -> run 1 yields 6, run 2 fetches remaining 4."""
    nb = SimpleNamespace(guid="nb-1", name="Engineering")
    notes = [
        SimpleNamespace(
            guid=f"note-{i}",
            title=f"Note {i}",
            notebookGuid="nb-1",
            updateSequenceNum=i,
            active=True,
        )
        for i in range(1, 11)
    ]
    chunk = SyncChunk(chunkHighUSN=10, updateCount=10, notebooks=[nb], notes=notes)
    client = FakeRateLimitEvernoteClient(
        chunks=[chunk],
        update_count=10,
        rate_limit_on_fetch_call=7,
    )
    state: dict[str, Any] = {}

    # Run 1: hits rate limit on 7th note body fetch
    rows1 = list(_sync_records(client, state, max_notes_per_run=100))
    data1 = [r for r in rows1 if isinstance(r, dict) and not r.get("_deleted")]

    assert len(data1) == 6
    assert [r["id"] for r in data1] == [f"note-{i}" for i in range(1, 7)]
    assert "last_usn" not in state
    assert "partial" in state
    assert state["partial"]["resume_usn"] == 6
    assert state["partial"]["full"] is True
    assert len(client.get_note_content_calls) == 7

    # Run 2: rate limit gone; pass 1 snapshot reloaded, pass 2 fetches ONLY notes 7..10
    chunk2 = SyncChunk(chunkHighUSN=10, updateCount=10, notebooks=[nb], notes=notes)
    client.chunks = [chunk2]
    client.rate_limit_on_fetch_call = None
    client.get_note_content_calls.clear()

    rows2 = list(_sync_records(client, state, max_notes_per_run=100))
    data2 = [r for r in rows2 if isinstance(r, dict) and not r.get("_deleted")]

    assert len(data2) == 4
    assert [r["id"] for r in data2] == [f"note-{i}" for i in range(7, 11)]
    assert client.get_note_content_calls == [f"note-{i}" for i in range(7, 11)]
    assert state["last_usn"] == 10
    assert "partial" not in state
    assert len(state["note_ids"]) == 10


def test_max_notes_per_run_batches_across_runs():
    """max_notes_per_run=4 over 10 notes completes across 3 runs (4+4+2 fetches)."""
    nb = SimpleNamespace(guid="nb-1", name="Engineering")
    notes = [
        SimpleNamespace(
            guid=f"note-{i}",
            title=f"Note {i}",
            notebookGuid="nb-1",
            updateSequenceNum=i,
            active=True,
        )
        for i in range(1, 11)
    ]

    client = FakeRateLimitEvernoteClient(
        chunks=[SyncChunk(chunkHighUSN=10, updateCount=10, notebooks=[nb], notes=notes)],
        update_count=10,
    )
    state: dict[str, Any] = {}

    # Run 1: fetches first 4 notes
    rows1 = list(_sync_records(client, state, max_notes_per_run=4))
    data1 = [r for r in rows1 if isinstance(r, dict) and not r.get("_deleted")]
    assert len(data1) == 4
    assert len(client.get_note_content_calls) == 4
    assert state["partial"]["resume_usn"] == 4
    assert "last_usn" not in state

    # Run 2: fetches next 4 notes (5..8)
    client.chunks = [SyncChunk(chunkHighUSN=10, updateCount=10, notebooks=[nb], notes=notes)]
    client.get_note_content_calls.clear()
    rows2 = list(_sync_records(client, state, max_notes_per_run=4))
    data2 = [r for r in rows2 if isinstance(r, dict) and not r.get("_deleted")]
    assert len(data2) == 4
    assert [r["id"] for r in data2] == [f"note-{i}" for i in range(5, 9)]
    assert len(client.get_note_content_calls) == 4
    assert state["partial"]["resume_usn"] == 8

    # Run 3: fetches remaining 2 notes (9..10)
    client.chunks = [SyncChunk(chunkHighUSN=10, updateCount=10, notebooks=[nb], notes=notes)]
    client.get_note_content_calls.clear()
    rows3 = list(_sync_records(client, state, max_notes_per_run=4))
    data3 = [r for r in rows3 if isinstance(r, dict) and not r.get("_deleted")]
    assert len(data3) == 2
    assert [r["id"] for r in data3] == ["note-9", "note-10"]
    assert len(client.get_note_content_calls) == 2
    assert "partial" not in state
    assert state["last_usn"] == 10


def test_edited_note_between_runs_is_refetched():
    """An edited note has higher USN and is fetched again; untouched earlier note is not."""
    nb = SimpleNamespace(guid="nb-1", name="Engineering")
    note1 = SimpleNamespace(
        guid="note-1", title="N1", notebookGuid="nb-1", updateSequenceNum=1, active=True
    )
    note2 = SimpleNamespace(
        guid="note-2", title="N2", notebookGuid="nb-1", updateSequenceNum=2, active=True
    )
    note3 = SimpleNamespace(
        guid="note-3", title="N3", notebookGuid="nb-1", updateSequenceNum=3, active=True
    )

    client = FakeRateLimitEvernoteClient(
        chunks=[
            SyncChunk(chunkHighUSN=3, updateCount=3, notebooks=[nb], notes=[note1, note2, note3])
        ],
        update_count=3,
    )
    state: dict[str, Any] = {}

    # Run 1: fetches note-1 and note-2, pauses before note-3
    rows1 = list(_sync_records(client, state, max_notes_per_run=2))
    assert len([r for r in rows1 if isinstance(r, dict) and not r.get("_deleted")]) == 2
    assert state["partial"]["resume_usn"] == 2

    # Between runs: note-1 is edited on server and assigned USN 4
    note1_edited = SimpleNamespace(
        guid="note-1", title="N1 Edited", notebookGuid="nb-1", updateSequenceNum=4, active=True
    )
    client.chunks = [
        SyncChunk(chunkHighUSN=4, updateCount=4, notebooks=[nb], notes=[note1_edited, note2, note3])
    ]
    client.update_count = 4
    client.get_note_content_calls.clear()

    # Run 2: note-2 (USN 2 <= 2) is skipped; note-3 (USN 3 > 2) and note-1 (USN 4 > 2) are fetched
    rows2 = list(_sync_records(client, state, max_notes_per_run=100))
    data2 = [r for r in rows2 if isinstance(r, dict) and not r.get("_deleted")]
    assert len(data2) == 2
    assert {r["id"] for r in data2} == {"note-1", "note-3"}
    assert "note-2" not in client.get_note_content_calls
    assert "partial" not in state
    assert state["last_usn"] == 4


def test_tombstones_emitted_during_unfinished_pass():
    """Tombstones are still emitted during an unfinished pass."""
    nb = SimpleNamespace(guid="nb-1", name="Engineering")
    note_active = SimpleNamespace(
        guid="note-act", title="Active", notebookGuid="nb-1", updateSequenceNum=1, active=True
    )
    note_trashed = SimpleNamespace(
        guid="note-trash", title="Trash", notebookGuid="nb-1", updateSequenceNum=2, active=False
    )

    client = FakeRateLimitEvernoteClient(
        chunks=[
            SyncChunk(
                chunkHighUSN=2,
                updateCount=2,
                notebooks=[nb],
                notes=[note_active, note_trashed],
            )
        ],
        update_count=2,
    )
    state: dict[str, Any] = {}

    # max_notes_per_run=0 stops before fetching active note bodies
    rows = list(_sync_records(client, state, max_notes_per_run=0))
    tombstones = [r for r in rows if r.get("_deleted")]

    assert len(tombstones) == 1
    assert tombstones[0]["id"] == "note-trash"
    assert "partial" in state


def test_first_body_fetch_rate_limited_clean_exit(caplog):
    """The very first body fetch is rate limited: clean end, warning logged, no data rows."""
    nb = SimpleNamespace(guid="nb-1", name="Engineering")
    notes = [
        SimpleNamespace(
            guid=f"note-{i}",
            title=f"N{i}",
            notebookGuid="nb-1",
            updateSequenceNum=i,
            active=True,
        )
        for i in range(1, 4)
    ]
    client = FakeRateLimitEvernoteClient(
        chunks=[SyncChunk(chunkHighUSN=3, updateCount=3, notebooks=[nb], notes=notes)],
        update_count=3,
        rate_limit_on_fetch_call=1,
    )
    state: dict[str, Any] = {}

    rows = list(_sync_records(client, state, max_notes_per_run=100))
    data = [r for r in rows if isinstance(r, dict) and not r.get("_deleted")]

    assert data == []
    assert "Evernote rate limit reached while fetching note" in caplog.text
    assert state["partial"]["resume_usn"] == 0
    assert "last_usn" not in state


def test_rate_limit_in_pass_1_clean_exit_leaves_state_unchanged(caplog):
    """Rate limit in pass 1 ends cleanly without exception and leaves state untouched."""
    client = FakeRateLimitEvernoteClient(rate_limit_in_pass_1=True)
    state = {
        "v": 1,
        "account": "production:12345",
        "last_usn": 20,
        "last_sync_time": 1609459200000,
        "notebooks": {"nb-1": "Engineering"},
        "tags": {},
        "note_ids": {"n-1": "nb-1"},
    }
    initial_copy = dict(state)

    rows = list(_sync_records(client, state))
    data = [r for r in rows if isinstance(r, dict) and not r.get("_deleted")]

    assert data == []
    assert state == initial_copy
    assert "Evernote rate limit reached during pass 1" in caplog.text


def test_interrupted_scope_change_resync_continues_and_restarts_on_second_scope_change():
    """Interrupted scope-change resync continues on same scope; restarts on second scope change."""
    nb_b = SimpleNamespace(guid="nb-b", name="ScopeB")
    notes_b = [
        SimpleNamespace(
            guid=f"b-{i}",
            title=f"B{i}",
            notebookGuid="nb-b",
            updateSequenceNum=i,
            active=True,
        )
        for i in range(1, 5)
    ]

    client = FakeRateLimitEvernoteClient(
        chunks=[
            SyncChunk(
                chunkHighUSN=4,
                updateCount=4,
                notebooks=[nb_b],
                notes=notes_b,
            )
        ],
        update_count=4,
    )

    # State initially has ScopeA
    state: dict[str, Any] = {
        "v": 1,
        "account": "production:12345",
        "last_usn": 10,
        "last_sync_time": 1609459200000,
        "notebooks": {"nb-a": "ScopeA"},
        "tags": {},
        "note_ids": {"a-1": "nb-a", "a-2": "nb-a"},
        "scope": {
            "notebooks": ["ScopeA"],
            "tags": None,
            "notebook_guids": ["nb-a"],
            "tag_guids": None,
        },
    }

    # Run 1: scope changed to ScopeB, max_notes_per_run=2 pauses after 2 notes
    rows1 = list(_sync_records(client, state, notebook_names=["ScopeB"], max_notes_per_run=2))
    tombstones1 = [r for r in rows1 if r.get("_deleted")]
    data1 = [r for r in rows1 if isinstance(r, dict) and not r.get("_deleted")]

    # ScopeA notes were tombstoned by full resync reconciliation
    assert {t["id"] for t in tombstones1} == {"a-1", "a-2"}
    assert len(data1) == 2
    assert state["partial"]["resume_usn"] == 2
    assert state["partial"]["full"] is True
    assert state["partial"]["scope"] == {"notebooks": ["ScopeB"], "tags": None}

    # Run 2: continuing ScopeB with matching scope -> does NOT restart
    client.chunks = [
        SyncChunk(
            chunkHighUSN=4,
            updateCount=4,
            notebooks=[nb_b],
            notes=notes_b,
        )
    ]
    client.get_note_content_calls.clear()

    rows2 = list(_sync_records(client, state, notebook_names=["ScopeB"], max_notes_per_run=100))
    data2 = [r for r in rows2 if isinstance(r, dict) and not r.get("_deleted")]

    assert len(data2) == 2
    assert [r["id"] for r in data2] == ["b-3", "b-4"]
    assert client.get_note_content_calls == ["b-3", "b-4"]
    assert "partial" not in state
    assert state["scope"]["notebook_guids"] == ["nb-b"]

    # Scenario 7b: An interrupted run with ScopeB, followed by scope change to ScopeC
    state_interrupted: dict[str, Any] = {
        "v": 1,
        "account": "production:12345",
        "last_usn": 10,
        "notebooks": {"nb-b": "ScopeB"},
        "tags": {},
        "note_ids": {"b-1": "nb-b", "b-2": "nb-b"},
        "partial": {
            "resume_usn": 2,
            "full": True,
            "scope": {"notebooks": ["ScopeB"], "tags": None},
        },
    }

    nb_c = SimpleNamespace(guid="nb-c", name="ScopeC")
    notes_c = [
        SimpleNamespace(
            guid=f"c-{i}",
            title=f"C{i}",
            notebookGuid="nb-c",
            updateSequenceNum=i,
            active=True,
        )
        for i in range(1, 3)
    ]
    client_c = FakeRateLimitEvernoteClient(
        chunks=[
            SyncChunk(
                chunkHighUSN=2,
                updateCount=2,
                notebooks=[nb_c],
                notes=notes_c,
            )
        ],
        update_count=2,
    )

    # Calling with ScopeC discards the ScopeB partial and starts fresh from USN 0
    rows_c = list(
        _sync_records(client_c, state_interrupted, notebook_names=["ScopeC"], max_notes_per_run=100)
    )
    data_c = [r for r in rows_c if isinstance(r, dict) and not r.get("_deleted")]
    assert len(data_c) == 2
    assert [r["id"] for r in data_c] == ["c-1", "c-2"]
    assert "partial" not in state_interrupted
    assert state_interrupted["scope"]["notebook_guids"] == ["nb-c"]


def test_empty_configured_scope_second_run_is_noop():
    """Empty configured list (notebook_names=[] or tag_names=[]) normalizes; run 2 is no-op."""
    nb = SimpleNamespace(guid="nb-1", name="Engineering")
    note = SimpleNamespace(
        guid="note-1", title="N1", notebookGuid="nb-1", updateSequenceNum=1, active=True
    )
    client = FakeRateLimitEvernoteClient(
        chunks=[SyncChunk(chunkHighUSN=10, updateCount=10, notebooks=[nb], notes=[note])],
        update_count=10,
    )
    state: dict[str, Any] = {}

    # Run 1: with empty lists
    rows1 = list(_sync_records(client, state, notebook_names=[], tag_names=[]))
    assert len([r for r in rows1 if isinstance(r, dict) and not r.get("_deleted")]) == 1
    assert state["scope"] == {
        "notebooks": None,
        "tags": None,
        "notebook_guids": None,
        "tag_guids": None,
    }
    assert len(client.get_filtered_sync_chunk_calls) == 1

    # Run 2: server unchanged at updateCount 10
    rows2 = list(_sync_records(client, state, notebook_names=[], tag_names=[]))
    data2 = [r for r in rows2 if isinstance(r, dict) and not r.get("_deleted")]
    assert data2 == []
    # No new chunk calls made on run 2
    assert len(client.get_filtered_sync_chunk_calls) == 1
