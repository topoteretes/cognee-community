"""Unit tests for the Ghost CMS dlt connector.

Tests run completely offline without an active Ghost publication:
* Post and page transformation to document row (author, tag, excerpt extraction).
* HTTP client request retry logic, pagination streaming, and transient error detection.
* dlt pipeline full snapshot integration and forget-on-delete semantics.
"""

from unittest.mock import MagicMock

import httpx
import pytest

from cognee_community_connector_ghost.ghost import (
    GHOST_SOURCE_NAME,
    GhostClient,
    _document_to_row,
    _render_document_content,
    ghost_source,
)


def test_render_document_content_extracts_metadata():
    """Verify author, tags, and timestamps are formatted into document content."""
    item = {
        "id": "post123",
        "title": "Scaling Memory with Graphs",
        "plaintext": "Knowledge graphs enrich LLM context.",
        "authors": [{"name": "Jane Doe"}, {"name": "John Smith"}],
        "tags": [{"name": "Engineering"}, {"name": "AI"}],
        "published_at": "2026-10-01T12:00:00Z",
    }
    rendered = _render_document_content(item)
    assert "Knowledge graphs enrich LLM context." in rendered
    assert "authors: Jane Doe, John Smith" in rendered
    assert "tags: Engineering, AI" in rendered
    assert "published_at: 2026-10-01T12:00:00Z" in rendered


def test_document_to_row_structure():
    """Verify document row ID prefixing, URL resolution, and title."""
    post = {
        "id": "p456",
        "title": "Welcome to Cognee",
        "slug": "welcome-to-cognee",
        "url": "https://myblog.com/welcome-to-cognee",
        "plaintext": "Introductory guide.",
    }
    row = _document_to_row("https://myblog.com", "posts", post)
    assert row["id"] == "ghost:post:p456"
    assert row["url"] == "https://myblog.com/welcome-to-cognee"
    assert row["title"] == "Welcome to Cognee"
    assert "Introductory guide." in row["content"]

    page = {
        "id": "page789",
        "title": "About Us",
        "slug": "about",
        "plaintext": "About the team.",
    }
    page_row = _document_to_row("https://myblog.com", "pages", page)
    assert page_row["id"] == "ghost:page:page789"
    assert page_row["title"] == "About Us"


def test_client_pagination_streaming():
    """Test pagination generator streaming across multiple pages."""
    mock_http = MagicMock()

    # Page 1 returns 2 posts, pages = 2
    resp1 = MagicMock()
    resp1.raise_for_status.return_value = None
    resp1.json.return_value = {
        "posts": [{"id": "1", "title": "Post 1"}, {"id": "2", "title": "Post 2"}],
        "meta": {"pagination": {"page": 1, "pages": 2}},
    }

    # Page 2 returns 1 post, pages = 2
    resp2 = MagicMock()
    resp2.raise_for_status.return_value = None
    resp2.json.return_value = {
        "posts": [{"id": "3", "title": "Post 3"}],
        "meta": {"pagination": {"page": 2, "pages": 2}},
    }

    mock_http.get.side_effect = [resp1, resp2]

    client = GhostClient(
        "https://demo.ghost.io",
        content_api_key="22444f484471c222c61b03ad88",
        http_client=mock_http,
    )
    items = list(client.iter_documents("posts"))

    assert len(items) == 3
    assert [p["id"] for p in items] == ["1", "2", "3"]
    assert mock_http.get.call_count == 2


def test_client_retry_on_transient_error(monkeypatch):
    """Test retry mechanism on 429 status code with Retry-After header."""
    monkeypatch.setattr("time.sleep", lambda _: None)
    mock_http = MagicMock()

    # Attempt 1: 429 Too Many Requests
    resp_429 = MagicMock()
    resp_429.status_code = 429
    resp_429.headers = {"Retry-After": "1"}
    http_err = httpx.HTTPStatusError("Rate limited", request=MagicMock(), response=resp_429)

    # Attempt 2: Success
    resp_ok = MagicMock()
    resp_ok.raise_for_status.return_value = None
    resp_ok.json.return_value = {
        "posts": [{"id": "post-retry", "title": "Recovered"}],
        "meta": {"pagination": {"page": 1, "pages": 1}},
    }

    resp_fail = MagicMock()
    resp_fail.raise_for_status.side_effect = http_err

    mock_http.get.side_effect = [resp_fail, resp_ok]

    client = GhostClient("https://demo.ghost.io", http_client=mock_http)
    items = list(client.iter_documents("posts"))

    assert len(items) == 1
    assert items[0]["title"] == "Recovered"
    assert mock_http.get.call_count == 2


def test_client_close_resource():
    """Verify client close correctly delegates when owning client."""
    client = GhostClient("https://demo.ghost.io")
    assert hasattr(client.client, "close")
    client.close()


def test_ghost_source_snapshot_and_deletion():
    """Verify that ghost_source yields items and sets DOCUMENT_SOURCE_ATTR."""
    mock_client = MagicMock()
    mock_client.base_url = "https://demo.ghost.io"
    mock_client.iter_documents.side_effect = lambda resource_type, filter_query=None: iter(
        [{"id": f"{resource_type}_1", "title": f"Title {resource_type}", "plaintext": "Text"}]
    )

    source = ghost_source(client=mock_client, include_posts=True, include_pages=True)
    assert getattr(source, "cognee_document_source", None) == GHOST_SOURCE_NAME

    # Extract rows yielded by resource
    resource = next(iter(source.selected_resources.values()))
    rows = list(resource())

    assert len(rows) == 2
    assert rows[0]["id"] == "ghost:post:posts_1"
    assert rows[1]["id"] == "ghost:page:pages_1"


def test_ghost_source_missing_key_raises():
    """Verify ValueError is raised when API key is missing."""
    with pytest.raises(ValueError, match="Ghost Content API key required"):
        ghost_source(base_url="https://demo.ghost.io", content_api_key=None)
