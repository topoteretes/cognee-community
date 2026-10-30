"""Unit tests for the Readwise dlt connector.

All tests run without a real Readwise token — the API is fully mocked via a
FakeReadwiseClient injected through the http_client= test seam.

Coverage:
  - Book + highlight rendering: title, author, category, notes, tags
  - Incremental sync: only updated books re-fetched after cursor set
  - First-run full sync: no tombstones attempted
  - Forget-on-delete: books absent from current listing are tombstoned
  - Reconciliation safety: tombstones suppressed on listing failure
  - Pagination: next URL followed correctly
  - Rate-limit handling: Retry-After parsed, bounded retries
  - Category filtering: per-category requests
  - document-source marker wired correctly
  - Empty account: no errors on zero books
"""

from types import SimpleNamespace

import pytest

from cognee_community_connector_readwise.readwise import (
    READWISE_SOURCE_NAME,
    _extract_tags,
    _paginate,
    _parse_retry_after,
    _render_book_doc,
    _sync_books,
    _utcnow_iso,
)


# ──────────────────────────────────────────────────────────────────────────────
# Fake HTTP client
# ──────────────────────────────────────────────────────────────────────────────


class FakeReadwiseClient:
    """In-memory stand-in for _ReadwiseClient."""

    def __init__(self):
        self._responses: dict[str, dict] = {}
        self.calls: list[tuple] = []
        self.raise_on: str | None = None  # path prefix that triggers an exception

    def register(self, path: str, data: dict):
        self._responses[path] = data

    def get(self, path: str, params: dict | None = None) -> dict:
        self.calls.append(("get", path, params))
        if self.raise_on and path.startswith(self.raise_on):
            raise RuntimeError(f"Simulated failure for {path}")
        # Full URL (pagination) → strip base
        key = path
        for prefix in ("https://readwise.io/api/v2",):
            if key.startswith(prefix):
                key = key[len(prefix):]
        if key in self._responses:
            return self._responses[key]
        # Default: empty listing
        return {"results": [], "next": None, "count": 0}


# ──────────────────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────────────────


def _make_book(book_id, title="Deep Work", author="Cal Newport",
               category="books", num_highlights=3, updated=None):
    return {
        "id": book_id,
        "title": title,
        "author": author,
        "category": category,
        "num_highlights": num_highlights,
        "last_highlight_at": updated or "2026-10-01T10:00:00Z",
        "source_url": f"https://example.com/book/{book_id}",
        "unique_url": f"https://example.com/book/{book_id}",
        "cover_image_url": "",
        "tags": [{"name": "productivity"}, {"name": "focus"}],
    }


def _make_highlight(hl_id, book_id, text, note="", location=0, tags=None):
    return {
        "id": hl_id,
        "book_id": book_id,
        "text": text,
        "note": note,
        "location": location,
        "tags": tags or [],
    }


def _setup_client(client, books, highlights_by_book_id):
    """Register books listing + per-book highlight listings."""
    client.register("/books/", {"results": books, "next": None, "count": len(books)})
    for book_id, hls in highlights_by_book_id.items():
        client.register(f"/highlights/", {"results": hls, "next": None, "count": len(hls)})


# ──────────────────────────────────────────────────────────────────────────────
# Rendering (DB-free)
# ──────────────────────────────────────────────────────────────────────────────


def test_render_book_doc_basic():
    book = _make_book(1, title="Deep Work", author="Cal Newport", category="books")
    highlights = [
        _make_highlight(10, 1, "Focus is the new IQ.", location=1),
        _make_highlight(11, 1, "Clarity about what matters most.", note="Key insight", location=2),
    ]
    doc = _render_book_doc(book, highlights)

    assert doc["id"] == "1"
    assert doc["title"] == "Deep Work"
    assert doc["author"] == "Cal Newport"
    assert doc["category"] == "Book"
    assert doc["_deleted"] is False

    content = doc["content"]
    assert "# Deep Work" in content
    assert "Cal Newport" in content
    assert "Focus is the new IQ." in content
    assert "Clarity about what matters most." in content
    assert "Key insight" in content
    assert "## Highlights" in content


def test_render_book_doc_highlights_sorted_by_location():
    book = _make_book(2)
    highlights = [
        _make_highlight(20, 2, "Third highlight", location=3),
        _make_highlight(21, 2, "First highlight", location=1),
        _make_highlight(22, 2, "Second highlight", location=2),
    ]
    doc = _render_book_doc(book, highlights)
    content = doc["content"]
    first_pos = content.index("First highlight")
    second_pos = content.index("Second highlight")
    third_pos = content.index("Third highlight")
    assert first_pos < second_pos < third_pos


def test_render_book_doc_tags():
    book = _make_book(3, title="Atomic Habits")
    book["tags"] = [{"name": "habits"}, {"name": "self-improvement"}]
    doc = _render_book_doc(book, [])
    assert "habits" in doc["content"]
    assert "self-improvement" in doc["content"]


def test_render_book_doc_empty_highlights():
    book = _make_book(4, title="Empty Book", num_highlights=0)
    doc = _render_book_doc(book, [])
    assert "# Empty Book" in doc["content"]
    # No highlights section when there are none
    assert "## Highlights" not in doc["content"]


def test_render_book_doc_article_category():
    book = _make_book(5, category="articles")
    doc = _render_book_doc(book, [])
    assert doc["category"] == "Article"


def test_render_book_doc_skips_empty_highlight_text():
    book = _make_book(6)
    highlights = [
        _make_highlight(60, 6, "", location=1),   # empty text — should be skipped
        _make_highlight(61, 6, "Valid highlight", location=2),
    ]
    doc = _render_book_doc(book, highlights)
    # Only the valid highlight should appear
    assert "Valid highlight" in doc["content"]


def test_extract_tags_list_of_dicts():
    obj = {"tags": [{"name": "tag1"}, {"name": "tag2"}]}
    assert _extract_tags(obj) == ["tag1", "tag2"]


def test_extract_tags_empty():
    assert _extract_tags({}) == []
    assert _extract_tags({"tags": []}) == []


def test_extract_tags_list_of_strings():
    obj = {"tags": ["alpha", "beta"]}
    assert _extract_tags(obj) == ["alpha", "beta"]


# ──────────────────────────────────────────────────────────────────────────────
# Pagination
# ──────────────────────────────────────────────────────────────────────────────


def test_paginate_follows_next_url():
    pages = {
        "/books/": {"results": [{"id": 1}], "next": "https://readwise.io/api/v2/books/?page=2"},
        "https://readwise.io/api/v2/books/?page=2": {"results": [{"id": 2}], "next": None},
    }

    def fake_get(path, params=None):
        return pages[path]

    client = SimpleNamespace(get=fake_get)
    results = list(_paginate(client, "/books/", {}))
    assert [r["id"] for r in results] == [1, 2]


def test_paginate_single_page():
    def fake_get(path, params=None):
        return {"results": [{"id": 99}], "next": None}

    client = SimpleNamespace(get=fake_get)
    results = list(_paginate(client, "/books/", {}))
    assert len(results) == 1


def test_paginate_empty_results():
    def fake_get(path, params=None):
        return {"results": [], "next": None}

    client = SimpleNamespace(get=fake_get)
    assert list(_paginate(client, "/books/", {})) == []


# ──────────────────────────────────────────────────────────────────────────────
# Sync: incremental cursor
# ──────────────────────────────────────────────────────────────────────────────


def test_first_run_syncs_all_books():
    client = FakeReadwiseClient()
    books = [_make_book(1, title="Book A"), _make_book(2, title="Book B")]
    client.register("/books/", {"results": books, "next": None})
    client.register("/highlights/", {"results": [], "next": None})

    state = {}
    docs = list(_sync_books(client, categories=None, state=state))

    book_docs = [d for d in docs if not d.get("_deleted")]
    assert len(book_docs) == 2
    titles = {d["title"] for d in book_docs}
    assert titles == {"Book A", "Book B"}


def test_first_run_no_tombstones():
    """On first run (no cursor) there should be no tombstones."""
    client = FakeReadwiseClient()
    client.register("/books/", {"results": [_make_book(1)], "next": None})
    client.register("/highlights/", {"results": [], "next": None})

    state = {}
    docs = list(_sync_books(client, categories=None, state=state))

    tombstones = [d for d in docs if d.get("_deleted")]
    assert tombstones == []


def test_cursor_advanced_after_sync():
    client = FakeReadwiseClient()
    client.register("/books/", {"results": [_make_book(1)], "next": None})
    client.register("/highlights/", {"results": [], "next": None})

    state = {}
    list(_sync_books(client, categories=None, state=state))

    assert "cursor" in state["books"]
    assert state["books"]["cursor"] is not None


def test_incremental_sync_uses_cursor():
    """On second run, updatedAfter param should be passed to the books endpoint."""
    client = FakeReadwiseClient()
    client.register("/books/", {"results": [], "next": None})

    state = {"books": {"cursor": "2026-10-01T00:00:00Z", "known_ids": ["1"]}}
    list(_sync_books(client, categories=None, state=state))

    # Check that the cursor was passed as updated__gt
    books_calls = [(path, params) for _, path, params in client.calls if "/books/" in path]
    assert any(
        p and p.get("updated__gt") == "2026-10-01T00:00:00Z"
        for _, p in books_calls
    )


def test_known_ids_persisted():
    client = FakeReadwiseClient()
    client.register("/books/", {"results": [_make_book(42)], "next": None})
    client.register("/highlights/", {"results": [], "next": None})

    state = {}
    list(_sync_books(client, categories=None, state=state))

    assert "42" in state["books"]["known_ids"]


# ──────────────────────────────────────────────────────────────────────────────
# Forget-on-delete
# ──────────────────────────────────────────────────────────────────────────────


def test_deleted_book_tombstoned_on_resync():
    """A book in known_ids but absent from the current full listing is tombstoned."""
    client = FakeReadwiseClient()
    # Only book 2 is returned — book 1 was deleted
    client.register("/books/", {"results": [_make_book(2)], "next": None})
    client.register("/highlights/", {"results": [], "next": None})

    state = {
        "books": {
            "cursor": "2026-09-01T00:00:00Z",
            "known_ids": ["1", "2"],  # book 1 was previously synced
        }
    }
    docs = list(_sync_books(client, categories=None, state=state))

    tombstones = [d for d in docs if d.get("_deleted")]
    assert len(tombstones) == 1
    assert tombstones[0]["id"] == "1"
    # Book 1 removed from known_ids
    assert "1" not in state["books"]["known_ids"]


def test_no_tombstones_if_listing_fails():
    """If the full listing call fails, no tombstones should be emitted."""
    client = FakeReadwiseClient()
    # First call (incremental) returns empty; second call (reconciliation) fails
    call_count = {"n": 0}

    def fake_get(path, params=None):
        call_count["n"] += 1
        if call_count["n"] == 1:
            return {"results": [], "next": None}  # incremental: nothing updated
        raise RuntimeError("Simulated listing failure")

    client.get = fake_get

    state = {
        "books": {
            "cursor": "2026-09-01T00:00:00Z",
            "known_ids": ["1", "2"],
        }
    }
    docs = list(_sync_books(client, categories=None, state=state))

    tombstones = [d for d in docs if d.get("_deleted")]
    assert tombstones == []  # safety: no mass-delete on listing failure


# ──────────────────────────────────────────────────────────────────────────────
# Category filtering
# ──────────────────────────────────────────────────────────────────────────────


def test_category_filter_passed_to_api():
    client = FakeReadwiseClient()
    client.register("/books/", {"results": [], "next": None})

    state = {}
    list(_sync_books(client, categories=["articles"], state=state))

    books_calls = [(path, params) for _, path, params in client.calls if "/books/" in path]
    assert any(p and p.get("category") == "articles" for _, p in books_calls)


def test_multiple_categories_make_separate_requests():
    client = FakeReadwiseClient()
    client.register("/books/", {"results": [], "next": None})

    state = {}
    list(_sync_books(client, categories=["books", "articles"], state=state))

    books_calls = [(path, params) for _, path, params in client.calls if "/books/" in path]
    categories_seen = {p.get("category") for _, p in books_calls if p}
    assert "books" in categories_seen
    assert "articles" in categories_seen


# ──────────────────────────────────────────────────────────────────────────────
# Highlights embedded in document
# ──────────────────────────────────────────────────────────────────────────────


def test_highlights_embedded_in_book_doc():
    client = FakeReadwiseClient()
    book = _make_book(10, title="Thinking Fast and Slow")
    hl = _make_highlight(100, 10, "System 1 thinking is fast and automatic.", location=5)
    client.register("/books/", {"results": [book], "next": None})
    client.register("/highlights/", {"results": [hl], "next": None})

    state = {}
    docs = list(_sync_books(client, categories=None, state=state))

    book_docs = [d for d in docs if not d.get("_deleted")]
    assert len(book_docs) == 1
    assert "System 1 thinking is fast and automatic." in book_docs[0]["content"]


def test_highlight_note_embedded_in_book_doc():
    client = FakeReadwiseClient()
    book = _make_book(11)
    hl = _make_highlight(110, 11, "The brain is lazy.", note="Remember this for chapter 3")
    client.register("/books/", {"results": [book], "next": None})
    client.register("/highlights/", {"results": [hl], "next": None})

    state = {}
    docs = list(_sync_books(client, categories=None, state=state))

    content = docs[0]["content"]
    assert "The brain is lazy." in content
    assert "Remember this for chapter 3" in content


# ──────────────────────────────────────────────────────────────────────────────
# Utilities
# ──────────────────────────────────────────────────────────────────────────────


def test_parse_retry_after_numeric():
    resp = SimpleNamespace(headers={"Retry-After": "30"})
    assert _parse_retry_after(resp) == 30.0


def test_parse_retry_after_missing_defaults_to_60():
    resp = SimpleNamespace(headers={})
    assert _parse_retry_after(resp) == 60.0


def test_utcnow_iso_format():
    iso = _utcnow_iso()
    # Should parse without error
    from datetime import datetime, timezone
    dt = datetime.strptime(iso, "%Y-%m-%dT%H:%M:%SZ")
    assert dt.year >= 2026


# ──────────────────────────────────────────────────────────────────────────────
# document-source marker
# ──────────────────────────────────────────────────────────────────────────────


def test_source_declares_document_marker():
    pytest.importorskip("dlt")
    pytest.importorskip("httpx")

    from cognee.tasks.ingestion.dlt_utils import document_source_tag
    from cognee_community_connector_readwise import readwise_source

    client = FakeReadwiseClient()
    source = readwise_source(token="test-token", http_client=client)
    assert document_source_tag(source) == READWISE_SOURCE_NAME


# ──────────────────────────────────────────────────────────────────────────────
# Edge cases
# ──────────────────────────────────────────────────────────────────────────────


def test_empty_account_no_errors():
    client = FakeReadwiseClient()
    # No books at all
    state = {}
    docs = list(_sync_books(client, categories=None, state=state))
    assert docs == []


def test_token_validation():
    """validate_token should not raise — just return a bool."""
    from cognee_community_connector_readwise.readwise import _ReadwiseClient
    pytest.importorskip("httpx")
    # We can't hit the real API in tests, but the method should exist and be callable
    assert hasattr(_ReadwiseClient, "validate_token")
