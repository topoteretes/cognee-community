"""Unit tests for Evernote incremental sync chunk iteration."""

import pytest
from evernote.edam.notestore.ttypes import SyncChunk, SyncChunkFilter

from cognee_community_connector_evernote import EvernoteError
from cognee_community_connector_evernote.evernote import iter_sync_chunks


class FakeChunkApi:
    def __init__(self, chunks):
        self.chunks = list(chunks)
        self.calls = []

    def get_filtered_sync_chunk(self, after_usn, max_entries, filter_obj):
        self.calls.append((after_usn, max_entries, filter_obj))
        if self.chunks:
            return self.chunks.pop(0)
        return SyncChunk(chunkHighUSN=None)


def test_single_chunk_iteration():
    """Single chunk with chunkHighUSN >= updateCount yields once and terminates."""
    chunk = SyncChunk(chunkHighUSN=100, updateCount=100, notes=["n1"])
    api = FakeChunkApi([chunk])

    chunks = list(iter_sync_chunks(api, after_usn=0, max_entries=250))

    assert len(chunks) == 1
    assert chunks[0] is chunk
    assert len(api.calls) == 1
    after_usn, max_entries, filter_obj = api.calls[0]
    assert after_usn == 0
    assert max_entries == 250
    assert isinstance(filter_obj, SyncChunkFilter)
    assert filter_obj.includeNotes is True
    assert filter_obj.includeNotebooks is True
    assert filter_obj.includeTags is True
    assert filter_obj.includeExpunged is True


def test_multiple_chunks_advance_after_usn():
    """Multiple chunks advance after_usn progressively until chunkHighUSN >= updateCount."""
    chunk1 = SyncChunk(chunkHighUSN=50, updateCount=120, notes=["n1"])
    chunk2 = SyncChunk(chunkHighUSN=100, updateCount=120, notes=["n2"])
    chunk3 = SyncChunk(chunkHighUSN=120, updateCount=120, notes=["n3"])
    api = FakeChunkApi([chunk1, chunk2, chunk3])

    chunks = list(iter_sync_chunks(api, after_usn=0, max_entries=50))

    assert len(chunks) == 3
    assert chunks == [chunk1, chunk2, chunk3]
    assert len(api.calls) == 3
    assert api.calls[0][0] == 0
    assert api.calls[1][0] == 50
    assert api.calls[2][0] == 100


def test_empty_chunk_terminates_immediately():
    """An empty chunk with chunkHighUSN=None yields nothing and terminates cleanly."""
    empty_chunk = SyncChunk(chunkHighUSN=None, updateCount=0)
    api = FakeChunkApi([empty_chunk])

    chunks = list(iter_sync_chunks(api, after_usn=0))

    assert len(chunks) == 0
    assert len(api.calls) == 1
    assert api.calls[0][0] == 0


def test_no_progress_guard_raises_evernote_error():
    """If a chunk's chunkHighUSN does not exceed previous after_usn, raise EvernoteError."""
    # Chunk with high USN equal to after_usn (no progress)
    stuck_chunk = SyncChunk(chunkHighUSN=50, updateCount=100)
    api = FakeChunkApi([stuck_chunk])

    with pytest.raises(EvernoteError, match=r"did not advance USN.*50 <= after_usn=50"):
        list(iter_sync_chunks(api, after_usn=50))


def test_no_progress_regression_raises_evernote_error():
    """If a chunk's chunkHighUSN regresses below after_usn, raise EvernoteError."""
    regressed_chunk = SyncChunk(chunkHighUSN=40, updateCount=100)
    api = FakeChunkApi([regressed_chunk])

    with pytest.raises(EvernoteError, match=r"did not advance USN.*40 <= after_usn=50"):
        list(iter_sync_chunks(api, after_usn=50))
