"""A fake EDAM NoteStore that models Evernote's USN change feed faithfully.

The connector's whole value is in *change detection* — what moved, what was
deleted, what was merely renamed — so the fake has to reproduce the real
protocol's semantics rather than return canned lists:

* every mutation bumps a monotonic account USN, and the note carries the USN of
  its last change (that is how a chunk decides "since you last looked");
* ``getSyncState`` returns the account's current USN, i.e. the target;
* ``getSyncChunk(after_usn)`` returns only notes changed after that USN, ascending,
  capped at ``max_entries``, and sets ``updateCount == chunkHighUSN`` when the
  client has caught up;
* trashed notes (``Note.deleted``) still arrive on the feed — Evernote's Trash is
  a real notebook, so a sync chunk does not hide them;
* expunged notes arrive only as bare GUIDs in ``expungedNotes`` and vanish from
  the feed entirely, which is why "absence" can never signal deletion.

No network, no credentials, no Evernote SDK.
"""

from types import SimpleNamespace

from cognee_community_connector_evernote.evernote import EvernoteNotFoundError


def make_note(
    guid,
    *,
    title="",
    notebook_guid="nb-default",
    tags=(),
    trashed=False,
    source_url=None,
    created=1_700_000_000_000,
    updated=1_700_000_000_000,
):
    """Build a note shaped like ``evernote.edam.type.ttypes.Note``."""
    return SimpleNamespace(
        guid=guid,
        title=title,
        content=None,  # sync chunks never carry content
        contentLength=0,
        created=created,
        updated=updated,
        deleted=1_700_000_000_000 if trashed else 0,
        active=not trashed,
        updateSequenceNum=0,
        notebookGuid=notebook_guid,
        tagGuids=[],
        tagNames=list(tags),
        resources=[],
        attributes=SimpleNamespace(sourceURL=source_url) if source_url else None,
    )


def make_notebook(guid, name):
    """Build a notebook shaped like ``evernote.edam.type.ttypes.Notebook``."""
    return SimpleNamespace(guid=guid, name=name, deleted=0)


def make_chunk(
    *,
    notes=(),
    notebooks=(),
    tags=(),
    expunged_notes=(),
    chunk_high_usn=0,
    update_count=None,
):
    """Build a chunk shaped like ``evernote.edam.notestore.ttypes.SyncChunk``."""
    return SimpleNamespace(
        currentTime=0,
        chunkHighUSN=chunk_high_usn,
        updateCount=chunk_high_usn if update_count is None else update_count,
        notes=list(notes),
        notebooks=list(notebooks),
        tags=list(tags),
        searches=[],
        resources=[],
        expungedNotes=list(expunged_notes),
        expungedNotebooks=[],
        expungedTags=[],
        linkedNotebooks=[],
        expungedLinkedNotebooks=[],
    )


class FakeEvernoteStore:
    """In-memory EDAM NoteStore with a real USN change feed."""

    def __init__(self, notebooks=None, max_entries=2):
        self.notebooks = dict(notebooks or {"nb-default": "Default"})
        self.max_entries = max_entries
        self.usn = 0
        self.notes = {}  # guid -> note (live *and* trashed)
        self.note_usn = {}  # guid -> USN of that note's last change
        self.content = {}  # guid -> ENML
        self.expunged = []  # (usn, guid) in deletion order
        self.calls = []  # (method, args) for assertions

    # -- account mutations (test setup) ------------------------------
    def add_note(self, guid, enml, **kwargs):
        note = make_note(guid, **kwargs)
        self.usn += 1
        self.notes[guid] = note
        self.content[guid] = enml
        self.note_usn[guid] = self.usn
        return note

    def edit_note(self, guid, enml=None, title=None, tags=None, notebook_guid=None):
        note = self.notes[guid]
        if enml is not None:
            self.content[guid] = enml
        if title is not None:
            note.title = title
        if tags is not None:
            note.tagNames = list(tags)
        if notebook_guid is not None:
            note.notebookGuid = notebook_guid
        self.usn += 1
        self.note_usn[guid] = self.usn
        return note

    def trash_note(self, guid):
        """Move to Trash — still on the feed, flagged ``deleted``."""
        note = self.notes[guid]
        note.deleted = self.usn + 1
        note.active = False
        self.usn += 1
        self.note_usn[guid] = self.usn
        return note

    def restore_note(self, guid):
        note = self.notes[guid]
        note.deleted = 0
        note.active = True
        self.usn += 1
        self.note_usn[guid] = self.usn
        return note

    def expunge_note(self, guid):
        """Permanent delete — disappears from the feed, appears in expungedNotes."""
        self.notes.pop(guid, None)
        self.content.pop(guid, None)
        self.note_usn.pop(guid, None)
        self.usn += 1
        self.expunged.append((self.usn, guid))
        return guid

    def rename_notebook(self, guid, name):
        self.notebooks[guid] = name
        self.usn += 1

    def set_chunk_limit(self, max_entries):
        """Shrink the page size so chunk paging can be exercised."""
        self.max_entries = max_entries

    # -- EDAM surface (the connector's dependency) --------------------
    def get_sync_state(self):
        self.calls.append(("get_sync_state", ()))
        return self.usn

    def get_sync_chunk(self, after_usn, max_entries):
        self.calls.append(("get_sync_chunk", (after_usn, max_entries)))

        changed = sorted(
            (
                (self.note_usn[guid], self.notes[guid])
                for guid in self.notes
                if self.note_usn[guid] > after_usn
            ),
            key=lambda pair: pair[0],
        )[: min(max_entries, self.max_entries)]

        notes = []
        for usn, note in changed:
            note.updateSequenceNum = usn
            notes.append(note)

        expunged = [guid for usn, guid in self.expunged if usn > after_usn]
        chunk_usn = max((usn for usn, _ in changed), default=after_usn)
        return make_chunk(
            notes=notes,
            expunged_notes=expunged,
            chunk_high_usn=chunk_usn,
            update_count=self.usn,
        )

    def get_note_content(self, guid):
        self.calls.append(("get_note_content", (guid,)))
        if guid not in self.content:
            raise EvernoteNotFoundError(f"note {guid} not found")
        return self.content[guid]

    def list_notebooks(self):
        self.calls.append(("list_notebooks", ()))
        return dict(self.notebooks)
