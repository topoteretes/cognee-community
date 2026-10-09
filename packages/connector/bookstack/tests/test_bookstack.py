"""Tests for the BookStack connector (mock-client based)."""

from __future__ import annotations

from typing import Any

import pytest
from cognee_community_connector_bookstack import bookstack_source


class _FakeClient:
    def __init__(self, responses: dict[str, Any] | None = None) -> None:
        self.responses = responses or {}
        self.calls: list[tuple[str, str]] = []

    def __call__(self, method: str, path: str, **kwargs: Any) -> Any:
        self.calls.append((method, path))
        key = f"{method} {path}"
        if key not in self.responses:
            raise KeyError(f"No canned response for {key}")
        return self.responses[key]


def test_source_factory_accepts_client_injection() -> None:
    fake = _FakeClient()
    source = bookstack_source(
        base_url="https://test.example.com",
        token_id="test",
        token_secret="test",
        client=fake,
    )
    from cognee.tasks.ingestion.dlt_utils import DOCUMENT_SOURCE_ATTR

    assert getattr(source, DOCUMENT_SOURCE_ATTR, None) == "bookstack"


def test_source_factory_requires_credentials() -> None:
    import os

    os.environ.pop("BOOKSTACK_BASE_URL", None)
    os.environ.pop("BOOKSTACK_TOKEN_ID", None)
    os.environ.pop("BOOKSTACK_TOKEN_SECRET", None)
    with pytest.raises(ValueError, match=r"BookStack base URL required"):
        bookstack_source(base_url=None, token_id=None, token_secret=None, client=None)


def test_page_to_row_builds_prose_body() -> None:
    from cognee_community_connector_bookstack.bookstack import _page_to_row

    def fake_client(method: str, path: str, **kwargs: Any) -> Any:
        return {"markdown": "# Welcome\n\nThis is a test page.\n"}

    page = {
        "id": 42,
        "book_id": 5,
        "chapter_id": None,
        "slug": "welcome-page",
        "name": "Welcome Page",
        "url": "https://wiki.example.com/books/mybook/page/welcome-page",
        "created_at": "2026-01-15T10:00:00Z",
        "updated_at": "2026-01-16T14:30:00Z",
        "book": {"name": "My Book"},
        "owned_by": {"name": "Admin User"},
    }

    row = _page_to_row(fake_client, page)
    assert row["id"] == 42
    assert row["name"] == "Welcome Page"
    assert "Welcome Page" in row["text"]
    assert "Book: My Book" in row["text"]
    assert "# Welcome" in row["text"]
    assert "This is a test page." in row["text"]


def test_page_to_row_handles_missing_markdown() -> None:
    from cognee_community_connector_bookstack.bookstack import _page_to_row

    def fake_client(method: str, path: str, **kwargs: Any) -> Any:
        raise RuntimeError("markdown unavailable")

    page = {
        "id": 99,
        "name": "Stub Page",
        "slug": "stub",
        "url": "https://wiki.example.com/pages/stub",
        "created_at": "2026-01-01T00:00:00Z",
        "updated_at": "2026-01-01T00:00:00Z",
    }

    row = _page_to_row(fake_client, page)
    assert row["id"] == 99
    assert "Stub Page" in row["text"]
    # Should still produce a valid row even without markdown content
    assert row["text"].strip() != ""
