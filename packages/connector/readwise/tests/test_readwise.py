"""Offline tests for the Readwise data-source connector."""

from datetime import UTC, datetime

import pytest

from cognee_community_connector_readwise.readwise import (
    _book_to_row,
    _iter_export,
    _render_book,
    _sync_rows,
)


class FakeResponse:
    def __init__(self, payload, status_code=200, headers=None):
        self.payload = payload
        self.status_code = status_code
        self.headers = headers or {}

    def json(self):
        return self.payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class FakeSession:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return next(self.responses)


def _book(book_id=123, **overrides):
    book = {
        "user_book_id": book_id,
        "is_deleted": False,
        "title": "Retrieval Notes",
        "author": "Ada Example",
        "category": "articles",
        "source": "reader",
        "source_url": "https://example.com/retrieval",
        "summary": "A short summary.",
        "document_note": "Compare sparse and dense retrieval.",
        "highlights": [
            {"id": 2, "location": 2, "text": "Second highlight", "note": "Useful"},
            {"id": 1, "location": 1, "text": "First highlight", "note": ""},
            {"id": 3, "location": 3, "text": "Deleted", "is_deleted": True},
        ],
    }
    book.update(overrides)
    return book


def test_iter_export_paginates_and_preserves_filters():
    session = FakeSession(
        [
            FakeResponse({"results": [_book(1)], "nextPageCursor": "next"}),
            FakeResponse({"results": [_book(2)], "nextPageCursor": None}),
        ]
    )

    rows = list(
        _iter_export(
            session,
            "secret",
            updated_after="2026-09-01T00:00:00Z",
            book_ids=[1, "2"],
        )
    )

    assert [row["user_book_id"] for row in rows] == [1, 2]
    assert session.calls[0][1]["headers"] == {"Authorization": "Token secret"}
    assert session.calls[0][1]["params"] == {
        "includeDeleted": "true",
        "updatedAfter": "2026-09-01T00:00:00Z",
        "ids": "1,2",
    }
    assert session.calls[1][1]["params"]["pageCursor"] == "next"


def test_iter_export_rejects_repeated_cursor():
    session = FakeSession(
        [
            FakeResponse({"results": [], "nextPageCursor": "same"}),
            FakeResponse({"results": [], "nextPageCursor": "same"}),
        ]
    )
    with pytest.raises(ValueError, match="repeated page cursor"):
        list(_iter_export(session, "secret"))


def test_book_to_row_renders_document_context_and_live_highlights():
    row = _book_to_row(_book())

    assert row["id"] == "123"
    assert row["url"] == "https://example.com/retrieval"
    assert row["title"] == "Retrieval Notes"
    assert row["_deleted"] is False
    assert "Author: Ada Example" in row["content"]
    assert "## Summary" in row["content"]
    assert "Compare sparse and dense retrieval." in row["content"]
    assert row["content"].index("First highlight") < row["content"].index("Second highlight")
    assert "Note: Useful" in row["content"]
    assert "Deleted" not in row["content"]


def test_deleted_book_becomes_minimal_hard_delete_row():
    assert _book_to_row(_book(is_deleted=True)) == {"id": "123", "_deleted": True}


def test_book_without_id_is_rejected():
    with pytest.raises(ValueError, match="user_book_id"):
        _book_to_row({})


def test_render_book_handles_empty_optional_fields():
    assert _render_book(_book(author=None, summary=None, document_note=None, highlights=[])) == (
        "Category: articles\nSource: reader"
    )


def test_sync_cursor_advances_only_after_complete_iteration():
    session = FakeSession([FakeResponse({"results": [_book()], "nextPageCursor": None})])
    state = {"updated_after": "2026-08-01T00:00:00Z"}
    generator = _sync_rows(
        session,
        "secret",
        state,
        clock=lambda: datetime(2026, 9, 3, 1, 2, 3, tzinfo=UTC),
    )

    first = next(generator)
    assert first["id"] == "123"
    assert state["updated_after"] == "2026-08-01T00:00:00Z"
    with pytest.raises(StopIteration):
        next(generator)
    assert state["updated_after"] == "2026-09-03T01:02:03Z"


def test_sync_failure_does_not_advance_cursor():
    session = FakeSession([FakeResponse({}, status_code=401)])
    state = {"updated_after": "old"}

    with pytest.raises(RuntimeError, match="HTTP 401"):
        list(_sync_rows(session, "secret", state))
    assert state == {"updated_after": "old"}
