"""Tests for the Readwise connector.

Everything runs offline against a fake Readwise client — no network, no token.
The dlt-wiring tests drive a real ``dlt`` merge against SQLite.
"""

import pytest

from cognee_community_connector_readwise.readwise import (
    READWISE_SOURCE_NAME,
    READWISE_TABLE_BOOKS,
    READWISE_TABLE_HIGHLIGHTS,
    ReadwiseClient,
    _book_to_row,
    _deleted_row,
    _highlight_to_row,
    readwise_source,
    sync_books,
    sync_highlights,
)
from cognee.tasks.ingestion.dlt_utils import DOCUMENT_SOURCE_ATTR


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------
def _make_book(book_id, title="Some Book", author="Some Author", category="books",
               num_highlights=0, updated="2026-01-01T00:00:00Z"):
    return {
        "id": book_id,
        "title": title,
        "author": author,
        "category": category,
        "source": "kindle",
        "num_highlights": num_highlights,
        "updated": updated,
        "source_url": f"https://example.com/{book_id}",
        "highlights_url": f"https://readwise.io/bookreview/{book_id}",
    }


def _make_highlight(highlight_id, book_id, text="A great passage",
                    note="", updated="2026-01-02T00:00:00Z", is_discard=False):
    return {
        "id": highlight_id,
        "text": text,
        "note": note,
        "book_id": book_id,
        "url": f"https://example.com/{book_id}",
        "highlight_url": f"https://readwise.io/open/{highlight_id}",
        "updated": updated,
        "is_discard": is_discard,
        "tags": [{"id": 1, "name": "deep-work"}],
    }


class FakeReadwiseClient:
    """In-memory stand-in for ReadwiseClient; records how it was called."""

    def __init__(self, books=(), highlights=()):
        self.books = list(books)
        self.highlights = list(highlights)
        self.calls = []

    def list_books(self, *, category=None):
        self.calls.append(("list_books", {"category": category}))
        books = self.books
        if category:
            books = [b for b in books if b.get("category") == category]
        yield from books

    def list_highlights(self, *, updated_after=None, book_id=None):
        self.calls.append(
            ("list_highlights", {"updated_after": updated_after, "book_id": book_id})
        )
        highlights = self.highlights
        if book_id is not None:
            highlights = [h for h in highlights if str(h.get("book_id")) == str(book_id)]
        if updated_after is not None:
            highlights = [h for h in highlights if (h.get("updated") or "") >= updated_after]
        yield from highlights


class _StubResponse:
    def __init__(self, status, payload=None, headers=None):
        self.status_code = status
        self._payload = payload if payload is not None else {}
        self.headers = headers or {}

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class _StubHttp:
    def __init__(self, responses):
        self._responses = list(responses)
        self.requests = []

    def get(self, path, params=None):
        self.requests.append((path, dict(params or {})))
        resp = self._responses.pop(0)
        if isinstance(resp, Exception):
            raise resp
        return resp


def _client_with_stub(responses):
    client = ReadwiseClient.__new__(ReadwiseClient)
    client._client = _StubHttp(responses)
    return client


# ---------------------------------------------------------------------------
# Row builders
# ---------------------------------------------------------------------------
def test_highlight_row_flattens_text_note_book_and_tags():
    book = _make_book(7, title="Deep Work", author="Cal Newport", category="books")
    highlight = _make_highlight(42, 7, text="Focus is a skill.", note="Practice daily")

    row = _highlight_to_row(highlight, book)

    assert row["id"] == "42"
    assert row["url"] == "https://readwise.io/open/42"
    assert row["title"] == "Deep Work — Cal Newport"
    assert row["content"] == "Focus is a skill.\n\nNote: Practice daily"
    assert row["book_id"] == "7"
    assert row["author"] == "Cal Newport"
    assert row["category"] == "books"
    assert row["tags"] == "deep-work"
    assert row["_deleted"] is False


def test_highlight_row_without_note_and_without_book():
    highlight = _make_highlight(1, 9, text="Just text", note="")

    row = _highlight_to_row(highlight, None)

    assert row["content"] == "Just text"
    assert row["title"] == "Unknown source"


def test_book_row():
    row = _book_to_row(_make_book(7, num_highlights=3))

    assert row["id"] == "7"
    assert row["title"] == "Some Book"
    assert "Some Author" in row["content"]
    assert "3 highlight(s)" in row["content"]
    assert row["_deleted"] is False


def test_deleted_row():
    assert _deleted_row(5) == {"id": "5", "_deleted": True}


# ---------------------------------------------------------------------------
# ReadwiseClient: pagination + retries (fake HTTP)
# ---------------------------------------------------------------------------
def test_list_highlights_follows_page_cursor():
    client = _client_with_stub(
        [
            _StubResponse(200, {"results": [_make_highlight(1, 1)], "nextPageCursor": "abc"}),
            _StubResponse(200, {"results": [_make_highlight(2, 1)], "nextPageCursor": None}),
        ]
    )

    highlights = list(client.list_highlights(updated_after="2026-01-01T00:00:00Z"))

    assert [h["id"] for h in highlights] == [1, 2]
    assert client._client.requests[0][1]["updatedAfter"] == "2026-01-01T00:00:00Z"
    assert client._client.requests[1][1]["pageCursor"] == "abc"


def test_list_books_follows_numbered_pages():
    client = _client_with_stub(
        [
            _StubResponse(200, {"results": [_make_book(1)], "next": "page2"}),
            _StubResponse(200, {"results": [_make_book(2)], "next": None}),
        ]
    )

    books = list(client.list_books(category="articles"))

    assert [b["id"] for b in books] == [1, 2]
    assert client._client.requests[0][1] == {"page_size": 100, "page": 1, "category": "articles"}
    assert client._client.requests[1][1]["page"] == 2


def test_get_retries_on_429_honoring_retry_after(monkeypatch):
    slept = []
    import cognee_community_connector_readwise.readwise as readwise_module

    class _NoSleep:
        def sleep(self, seconds):
            slept.append(seconds)

    monkeypatch.setattr(readwise_module, "time", _NoSleep())
    client = _client_with_stub(
        [
            _StubResponse(429, headers={"Retry-After": "7"}),
            _StubResponse(200, {"results": [], "nextPageCursor": None}),
        ]
    )

    assert list(client.list_highlights()) == []
    assert slept == [7.0]


def test_get_raises_after_exhausted_retries(monkeypatch):
    import cognee_community_connector_readwise.readwise as readwise_module

    class _NoSleep:
        def sleep(self, seconds):
            pass

    monkeypatch.setattr(readwise_module, "time", _NoSleep())
    client = _client_with_stub([_StubResponse(500)] * 5)

    with pytest.raises(RuntimeError):
        list(client.list_highlights())


# ---------------------------------------------------------------------------
# sync_books
# ---------------------------------------------------------------------------
def test_sync_books_first_run_yields_all_and_records_seen():
    client = FakeReadwiseClient(books=[_make_book(1), _make_book(2)])
    state = {}

    rows = list(sync_books(client, state))

    assert [r["id"] for r in rows] == ["1", "2"]
    assert all(r["_deleted"] is False for r in rows)
    assert state["seen_book_ids"] == ["1", "2"]


def test_sync_books_emits_deleted_for_vanished_book():
    client = FakeReadwiseClient(books=[_make_book(1), _make_book(2)])
    state = {}
    list(sync_books(client, state))

    client.books = [_make_book(1)]
    rows = list(sync_books(client, state))

    assert rows == [
        {"id": "1", "url": "https://example.com/1", "title": "Some Book",
         "content": "Some Book\nby Some Author\nCategory: books\n0 highlight(s)",
         "_deleted": False},
        {"id": "2", "_deleted": True},
    ]
    assert state["seen_book_ids"] == ["1"]


def test_sync_books_scope_book_ids():
    client = FakeReadwiseClient(books=[_make_book(1), _make_book(2)])
    state = {}

    rows = list(sync_books(client, state, book_ids=["2"]))

    assert [r["id"] for r in rows] == ["2"]
    assert state["seen_book_ids"] == ["2"]


# ---------------------------------------------------------------------------
# sync_highlights
# ---------------------------------------------------------------------------
def test_sync_highlights_first_run_is_full_backfill():
    book = _make_book(1, num_highlights=2)
    client = FakeReadwiseClient(
        books=[book],
        highlights=[_make_highlight(10, 1), _make_highlight(11, 1)],
    )
    state = {}

    rows = list(sync_highlights(client, state))

    assert [r["id"] for r in rows] == ["10", "11"]
    # No cursor on the first run -> no updatedAfter param.
    assert ("list_highlights", {"updated_after": None, "book_id": None}) in client.calls
    assert state["last_highlight_sync"]
    assert state["books"]["1"]["num_highlights"] == 2
    assert state["books"]["1"]["highlight_ids"] == ["10", "11"]


def test_sync_highlights_incremental_uses_cursor():
    client = FakeReadwiseClient(
        books=[_make_book(1, num_highlights=1)],
        highlights=[_make_highlight(10, 1, updated="2026-02-01T00:00:00Z")],
    )
    state = {}
    list(sync_highlights(client, state))
    cursor = state["last_highlight_sync"]
    client.calls.clear()

    # A highlight updated after the first run's cursor.
    client.highlights.append(_make_highlight(11, 1, updated="2099-03-01T00:00:00Z"))
    client.books[0]["num_highlights"] = 2
    rows = list(sync_highlights(client, state))

    assert [r["id"] for r in rows] == ["11"]
    highlight_calls = [c for c in client.calls if c[0] == "list_highlights"]
    assert highlight_calls[0][1]["updated_after"] == cursor
    assert cursor is not None


def test_sync_highlights_skips_discarded():
    client = FakeReadwiseClient(
        books=[_make_book(1, num_highlights=1)],
        highlights=[
            _make_highlight(10, 1),
            _make_highlight(11, 1, is_discard=True),
        ],
    )

    rows = list(sync_highlights(client, {}))

    assert [r["id"] for r in rows] == ["10"]


def test_sync_highlights_deleted_book_cascades_to_highlights():
    book1, book2 = _make_book(1, num_highlights=1), _make_book(2, num_highlights=1)
    client = FakeReadwiseClient(
        books=[book1, book2],
        highlights=[_make_highlight(10, 1), _make_highlight(20, 2)],
    )
    state = {}
    list(sync_highlights(client, state))

    # Book 2 vanishes upstream.
    client.books = [book1]
    rows = list(sync_highlights(client, state))

    assert {"id": "20", "_deleted": True} in rows
    assert "2" not in state["books"]


def test_sync_highlights_shrunk_book_reconciles_deleted_highlight():
    client = FakeReadwiseClient(
        books=[_make_book(1, num_highlights=2)],
        highlights=[_make_highlight(10, 1), _make_highlight(11, 1)],
    )
    state = {}
    list(sync_highlights(client, state))

    # Highlight 11 deleted in Readwise; the book now reports fewer highlights.
    client.books = [_make_book(1, num_highlights=1)]
    client.highlights = [_make_highlight(10, 1)]
    rows = list(sync_highlights(client, state))

    assert {"id": "11", "_deleted": True} in rows
    assert state["books"]["1"]["highlight_ids"] == ["10"]


def test_sync_highlights_category_scope():
    client = FakeReadwiseClient(
        books=[
            _make_book(1, category="books", num_highlights=1),
            _make_book(2, category="articles", num_highlights=1),
        ],
        highlights=[_make_highlight(10, 1), _make_highlight(20, 2)],
    )

    rows = list(sync_highlights(client, {}, category="books"))

    assert [r["id"] for r in rows] == ["10"]
    assert ("list_books", {"category": "books"}) in client.calls


# ---------------------------------------------------------------------------
# readwise_source (dlt wiring) — requires dlt
# ---------------------------------------------------------------------------
def _merge_disposition(schema):
    write_disposition = schema.get("write_disposition")
    if isinstance(write_disposition, dict):  # dlt may normalize to a config dict
        write_disposition = write_disposition.get("disposition")
    return write_disposition


def test_readwise_source_sets_document_source_attr():
    pytest.importorskip("dlt")

    source = readwise_source(client=FakeReadwiseClient())

    assert getattr(source, DOCUMENT_SOURCE_ATTR) == READWISE_SOURCE_NAME


def test_readwise_source_resources_are_merge_with_hard_delete():
    pytest.importorskip("dlt")

    source = readwise_source(client=FakeReadwiseClient())
    resources = {r.name: r for r in source.resources.values()}

    assert set(resources) == {READWISE_TABLE_BOOKS, READWISE_TABLE_HIGHLIGHTS}
    for resource in resources.values():
        schema = resource.compute_table_schema()
        assert _merge_disposition(schema) == "merge"
        assert schema["columns"]["id"].get("primary_key") is True
        assert schema["columns"]["_deleted"].get("hard_delete") is True


def test_readwise_source_requires_token(monkeypatch):
    pytest.importorskip("dlt")
    monkeypatch.delenv("READWISE_API_KEY", raising=False)

    with pytest.raises(ValueError, match="READWISE_API_KEY"):
        readwise_source()


def test_readwise_source_requires_dlt(monkeypatch):
    import builtins

    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name == "dlt":
            raise ImportError("no dlt")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    with pytest.raises(ImportError, match=r"cognee\[readwise\]"):
        readwise_source(client=object())


def test_e2e_dlt_merge_hard_delete_removes_deleted_book_and_highlights(tmp_path):
    """End-to-end, offline (no LLM): drive readwise_source through a real dlt
    merge and prove _deleted markers physically remove rows from the
    destination — which is exactly what cognee's orphan_cleanup reconciles
    against.
    """
    dlt = pytest.importorskip("dlt")

    pipelines_dir = str(tmp_path / "dlt_pipelines")
    db_path = tmp_path / "readwise.db"

    def sync(client):
        pipeline = dlt.pipeline(
            pipeline_name="readwise_e2e_test",
            destination=dlt.destinations.sqlalchemy(f"sqlite:///{db_path}"),
            dataset_name="readwise_e2e",
            pipelines_dir=pipelines_dir,
        )
        pipeline.run(readwise_source(client=client))
        with pipeline.sql_client() as sql:
            books = sql.execute_sql(f"SELECT id FROM {READWISE_TABLE_BOOKS} ORDER BY id")
            highlights = sql.execute_sql(
                f"SELECT id FROM {READWISE_TABLE_HIGHLIGHTS} ORDER BY id"
            )
        return [r[0] for r in books], [r[0] for r in highlights]

    # Run 1 — backfill: 2 books, 3 highlights; the source-state cursor is stored.
    backfill = FakeReadwiseClient(
        books=[_make_book(1, num_highlights=2), _make_book(2, num_highlights=1)],
        highlights=[
            _make_highlight(10, 1, updated="2026-01-01T00:00:00Z"),
            _make_highlight(11, 1, updated="2026-01-01T00:00:00Z"),
            _make_highlight(20, 2, updated="2026-01-01T00:00:00Z"),
        ],
    )
    assert sync(backfill) == (["1", "2"], ["10", "11", "20"])

    # Run 2 — book 2 deleted upstream: its book row AND its highlight row must
    # physically disappear. Highlight 11 was deleted too (book 1 shrank, so the
    # reconciliation re-list catches it), and a brand-new book 3 arrives with a
    # highlight newer than the cursor (exercising the updatedAfter path).
    incremental = FakeReadwiseClient(
        books=[_make_book(1, num_highlights=1), _make_book(3, num_highlights=1)],
        highlights=[
            _make_highlight(10, 1, updated="2026-01-01T00:00:00Z"),
            # Updated after the first run's cursor (far-future date keeps the
            # offline fake's >= comparison honest).
            _make_highlight(30, 3, updated="2099-01-01T00:00:00Z"),
        ],
    )
    books, highlights = sync(incremental)
    assert books == ["1", "3"]
    assert sorted(highlights) == ["10", "30"]
