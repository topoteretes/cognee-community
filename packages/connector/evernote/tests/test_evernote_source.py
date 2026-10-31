"""Unit tests for evernote_source dlt resource construction and filtering logic.

All tests run offline using fake Evernote API clients.
"""

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from dlt.extract.resource import DltResource
from evernote.edam.notestore.ttypes import SyncChunk

from cognee_community_connector_evernote import (
    EvernoteError,
    NoteNotFoundError,
    evernote_source,
)


class FakeEvernoteClient:
    def __init__(self, chunks=None, note_contents=None, sync_state=None):
        self.chunks = list(chunks or [])
        self.note_contents = dict(note_contents or {})
        self.get_note_content_calls = []
        self.sync_state = sync_state

    def get_sync_state(self):
        if self.sync_state is not None:
            return self.sync_state
        return SimpleNamespace(currentTime=1600000000000, updateCount=1000)

    def get_user_id(self):
        return 12345

    def get_filtered_sync_chunk(self, after_usn, max_entries, filter_obj):
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
        return f"<en-note><div>Default content for {guid}</div></en-note>"


def test_missing_token_raises_value_error(monkeypatch):
    """When no token is provided and EVERNOTE_TOKEN is not set, ValueError is raised."""
    monkeypatch.delenv("EVERNOTE_TOKEN", raising=False)
    with pytest.raises(ValueError, match="Evernote authentication token required"):
        evernote_source()


def test_token_never_appears_in_repr_or_error(monkeypatch):
    """Sensitive token string must never appear in repr or error messages."""
    secret_token = "super-secret-token-12345-xyz"
    source = evernote_source(token=secret_token)
    assert secret_token not in repr(source)
    assert secret_token not in str(source)

    monkeypatch.delenv("EVERNOTE_TOKEN", raising=False)
    with pytest.raises(ValueError) as exc_info:
        evernote_source()
    assert secret_token not in str(exc_info.value)


def test_construction_rules_makes_no_network_call():
    """evernote_source constructs a DltResource without making any network calls."""
    fake_client = FakeEvernoteClient()
    fake_client.get_filtered_sync_chunk = MagicMock(side_effect=AssertionError("Network called!"))
    fake_client.get_note_content = MagicMock(side_effect=AssertionError("Network called!"))

    source = evernote_source(token="tok-123", client=fake_client)
    assert isinstance(source, DltResource)
    assert source.name == "evernote_notes"
    assert source.write_disposition == "merge"
    assert source.cognee_document_source == "evernote"
    assert source.cognee_pipeline_scope == "evernote_notes"
    assert source.client is fake_client
    # No network calls were triggered during construction
    fake_client.get_filtered_sync_chunk.assert_not_called()
    fake_client.get_note_content.assert_not_called()


def test_scope_validation_unknown_notebook_name_raises():
    """Requesting an unknown notebook name raises EvernoteError listing the missing name."""
    nb = SimpleNamespace(guid="nb-1", name="Engineering")
    chunk = SyncChunk(chunkHighUSN=10, updateCount=10, notebooks=[nb], notes=[])
    client = FakeEvernoteClient(chunks=[chunk])

    source = evernote_source(
        token="tok", notebook_names=["Marketing", "Engineering"], client=client
    )

    with pytest.raises(Exception, match=r"Unknown notebook name\(s\): Marketing") as exc_info:
        list(source)
    assert isinstance(exc_info.value.__cause__, EvernoteError) or isinstance(
        exc_info.value, EvernoteError
    )


def test_scope_validation_unknown_tag_name_raises():
    """Requesting an unknown tag name raises EvernoteError listing the missing name."""
    tag = SimpleNamespace(guid="t-1", name="Urgent")
    chunk = SyncChunk(chunkHighUSN=10, updateCount=10, tags=[tag], notes=[])
    client = FakeEvernoteClient(chunks=[chunk])

    source = evernote_source(token="tok", tag_names=["Urgent", "Archive"], client=client)

    with pytest.raises(Exception, match=r"Unknown tag name\(s\): Archive") as exc_info:
        list(source)
    assert isinstance(exc_info.value.__cause__, EvernoteError) or isinstance(
        exc_info.value, EvernoteError
    )


def test_scope_filtering_rows():
    """Only active notes within the requested notebook and tag scopes are yielded as active rows."""
    nb1 = SimpleNamespace(guid="nb-1", name="Work")
    nb2 = SimpleNamespace(guid="nb-2", name="Personal")
    tag1 = SimpleNamespace(guid="t-1", name="Projects")
    tag2 = SimpleNamespace(guid="t-2", name="Misc")

    note_in_scope = SimpleNamespace(
        guid="n-in",
        title="Work Project Note",
        notebookGuid="nb-1",
        tagGuids=["t-1"],
        active=True,
        created=1609459200000,
        updated=1609459200000,
    )
    note_out_scope = SimpleNamespace(
        guid="n-out",
        title="Personal Misc Note",
        notebookGuid="nb-2",
        tagGuids=["t-2"],
        active=True,
        created=1609459200000,
        updated=1609459200000,
    )

    chunk = SyncChunk(
        chunkHighUSN=10,
        updateCount=10,
        notebooks=[nb1, nb2],
        tags=[tag1, tag2],
        notes=[note_in_scope, note_out_scope],
    )
    client = FakeEvernoteClient(
        chunks=[chunk],
        note_contents={"n-in": "<en-note><div>Work project content</div></en-note>"},
    )

    source = evernote_source(
        token="tok",
        notebook_names=["Work"],
        tag_names=["Projects"],
        client=client,
    )
    rows = list(source)

    active_rows = [r for r in rows if not r.get("_deleted")]
    tombstone_rows = [r for r in rows if r.get("_deleted")]

    assert len(active_rows) == 1
    assert active_rows[0]["id"] == "n-in"
    assert "Notebook: Work" in active_rows[0]["content"]
    assert "Tags: Projects" in active_rows[0]["content"]
    assert "Work project content" in active_rows[0]["content"]

    assert len(tombstone_rows) == 1
    assert tombstone_rows[0]["id"] == "n-out"
    assert tombstone_rows[0]["_deleted"] is True


def test_out_of_scope_notes_yield_tombstones():
    """Active notes outside the selected notebook or tag scope yield tombstones."""
    nb1 = SimpleNamespace(guid="nb-1", name="Docs")
    nb2 = SimpleNamespace(guid="nb-2", name="Archive")
    note1 = SimpleNamespace(guid="n-1", title="Doc 1", notebookGuid="nb-1", active=True)
    note2 = SimpleNamespace(guid="n-2", title="Archive 1", notebookGuid="nb-2", active=True)

    chunk = SyncChunk(
        chunkHighUSN=10,
        updateCount=10,
        notebooks=[nb1, nb2],
        notes=[note1, note2],
    )
    client = FakeEvernoteClient(
        chunks=[chunk],
        note_contents={"n-1": "<en-note><div>Doc content</div></en-note>"},
    )

    source = evernote_source(token="tok", notebook_names=["Docs"], client=client)
    rows = list(source)

    row_by_id = {r["id"]: r for r in rows}
    assert row_by_id["n-1"]["_deleted"] is False
    assert row_by_id["n-2"]["_deleted"] is True


def test_trashed_and_expunged_notes_yield_tombstones():
    """Trashed notes (active False or deleted set) and expunged GUIDs yield tombstones."""
    note_active_false = SimpleNamespace(
        guid="n-trash1", title="Trashed 1", notebookGuid="nb-1", active=False
    )
    note_deleted_set = SimpleNamespace(
        guid="n-trash2", title="Trashed 2", notebookGuid="nb-1", active=True, deleted=1609459200000
    )

    chunk = SyncChunk(
        chunkHighUSN=10,
        updateCount=10,
        notes=[note_active_false, note_deleted_set],
        expungedNotes=["exp-1", "exp-2"],
    )
    client = FakeEvernoteClient(chunks=[chunk])

    source = evernote_source(token="tok", client=client)
    rows = list(source)

    deleted_ids = {r["id"] for r in rows if r.get("_deleted")}
    assert deleted_ids == {"n-trash1", "n-trash2", "exp-1", "exp-2"}
    assert all(r["_deleted"] is True for r in rows)


def test_note_not_found_yields_tombstone():
    """When get_note_content raises NoteNotFoundError, a tombstone row is yielded."""
    note = SimpleNamespace(
        guid="n-vanished", title="Vanished Note", notebookGuid="nb-1", active=True
    )
    chunk = SyncChunk(chunkHighUSN=10, updateCount=10, notes=[note])

    client = FakeEvernoteClient(
        chunks=[chunk],
        note_contents={"n-vanished": NoteNotFoundError("Note vanished")},
    )

    source = evernote_source(token="tok", client=client)
    rows = list(source)

    assert len(rows) == 1
    assert rows[0]["id"] == "n-vanished"
    assert rows[0]["_deleted"] is True


def test_abort_on_other_error():
    """Any error other than NoteNotFoundError during get_note_content aborts the run."""
    note = SimpleNamespace(guid="n-err", title="Error Note", notebookGuid="nb-1", active=True)
    chunk = SyncChunk(chunkHighUSN=10, updateCount=10, notes=[note])

    client = FakeEvernoteClient(
        chunks=[chunk],
        note_contents={"n-err": RuntimeError("Database disk failure")},
    )

    source = evernote_source(token="tok", client=client)
    with pytest.raises(Exception, match="Database disk failure") as exc_info:
        list(source)
    assert isinstance(exc_info.value.__cause__, RuntimeError) or isinstance(
        exc_info.value, RuntimeError
    )


def test_notebook_before_note_ordering_across_chunks():
    """Pass 1 collects all notebooks before Pass 2 resolves names for notes."""
    # Chunk 1 has the note; Chunk 2 has the notebook definition!
    note = SimpleNamespace(
        guid="n-cross",
        title="Cross Chunk Note",
        notebookGuid="nb-late",
        active=True,
    )
    chunk1 = SyncChunk(chunkHighUSN=5, updateCount=10, notes=[note])

    nb = SimpleNamespace(guid="nb-late", name="Late Notebook")
    chunk2 = SyncChunk(chunkHighUSN=10, updateCount=10, notebooks=[nb])

    client = FakeEvernoteClient(
        chunks=[chunk1, chunk2],
        note_contents={"n-cross": "<en-note><div>Body text</div></en-note>"},
    )

    source = evernote_source(token="tok", client=client)
    rows = list(source)

    assert len(rows) == 1
    assert rows[0]["id"] == "n-cross"
    assert "Notebook: Late Notebook" in rows[0]["content"]


def test_no_body_fetch_for_tombstoned_notes():
    """Trashed, expunged, or out-of-scope notes never trigger get_note_content."""
    nb1 = SimpleNamespace(guid="nb-1", name="Scope1")
    nb2 = SimpleNamespace(guid="nb-2", name="Scope2")

    note_out = SimpleNamespace(guid="n-out", title="Out", notebookGuid="nb-2", active=True)
    note_trash = SimpleNamespace(guid="n-trash", title="Trash", notebookGuid="nb-1", active=False)

    chunk = SyncChunk(
        chunkHighUSN=10,
        updateCount=10,
        notebooks=[nb1, nb2],
        notes=[note_out, note_trash],
        expungedNotes=["exp-1"],
    )
    client = FakeEvernoteClient(chunks=[chunk])

    source = evernote_source(token="tok", notebook_names=["Scope1"], client=client)
    rows = list(source)

    assert len(rows) == 3
    assert all(r["_deleted"] is True for r in rows)
    # get_note_content was NEVER called
    assert client.get_note_content_calls == []


def test_empty_content_fallback_to_title_or_skip():
    """Empty content uses title; if title is also empty, the note yields a tombstone."""
    note_title_only = SimpleNamespace(
        guid="n-title", title="Only Title", notebookGuid="nb-1", active=True
    )
    note_empty_all = SimpleNamespace(guid="n-empty", title="", notebookGuid="nb-1", active=True)

    chunk = SyncChunk(
        chunkHighUSN=10,
        updateCount=10,
        notes=[note_title_only, note_empty_all],
    )
    client = FakeEvernoteClient(
        chunks=[chunk],
        note_contents={
            "n-title": "<en-note></en-note>",
            "n-empty": "<en-note></en-note>",
        },
    )

    source = evernote_source(token="tok", client=client)
    rows = list(source)

    # note_empty_all yields a tombstone; note_title_only has its title as content
    assert len(rows) == 2
    active_rows = [r for r in rows if not r.get("_deleted")]
    tombstone_rows = [r for r in rows if r.get("_deleted")]
    assert len(active_rows) == 1
    assert active_rows[0]["id"] == "n-title"
    assert active_rows[0]["title"] == "Only Title"
    assert active_rows[0]["content"] == "Only Title"
    assert len(tombstone_rows) == 1
    assert tombstone_rows[0]["id"] == "n-empty"
    assert tombstone_rows[0]["_deleted"] is True


def test_empty_body_and_title_yields_tombstone():
    """A note whose ENML body and title are both empty yields a tombstone (not silently skipped)."""
    note_empty = SimpleNamespace(guid="n-void", title="", notebookGuid="nb-1", active=True)
    note_title = SimpleNamespace(
        guid="n-titleonly", title="Just A Title", notebookGuid="nb-1", active=True
    )

    chunk = SyncChunk(
        chunkHighUSN=10,
        updateCount=10,
        notes=[note_empty, note_title],
    )
    client = FakeEvernoteClient(
        chunks=[chunk],
        note_contents={
            "n-void": "<en-note></en-note>",  # empty ENML, empty title -> tombstone
            "n-titleonly": "<en-note></en-note>",  # empty ENML, non-empty title -> title as content
        },
    )

    source = evernote_source(token="tok", client=client)
    rows = list(source)

    tombstones = [r for r in rows if r.get("_deleted")]
    active = [r for r in rows if not r.get("_deleted")]

    # The all-empty note must appear as a tombstone
    assert any(r["id"] == "n-void" for r in tombstones), "n-void must yield a tombstone"
    assert tombstones[0]["title"] == ""
    assert tombstones[0]["content"] == ""

    # The title-only note must appear as an active row using the title as content
    assert len(active) == 1
    assert active[0]["id"] == "n-titleonly"
    assert active[0]["content"] == "Just A Title"


def test_trashed_note_not_in_state_still_yields_tombstone():
    """A trashed note that was NOT previously ingested still yields a tombstone on first run."""
    note_trashed = SimpleNamespace(
        guid="n-new-trash", title="New Trashed Note", notebookGuid="nb-1", active=False
    )
    chunk = SyncChunk(
        chunkHighUSN=10,
        updateCount=10,
        notes=[note_trashed],
    )
    client = FakeEvernoteClient(chunks=[chunk])

    # First run: empty state, note was never in state["note_ids"]
    source = evernote_source(token="tok", client=client)
    rows = list(source)

    assert len(rows) == 1
    assert rows[0]["id"] == "n-new-trash"
    assert rows[0]["_deleted"] is True
