"""Unit tests for the Cognee DEV.to connector."""

from unittest.mock import MagicMock, patch
import pytest
from cognee_community_connector_devto.devto import (
    _get_headers,
    devto_source,
    fetch_devto_article_by_id,
    fetch_devto_articles,
    fetch_devto_documents,
    format_article_to_document,
)

SAMPLE_ARTICLES = [
    {
        "id": 101,
        "title": "Mastering Knowledge Graphs with Cognee",
        "description": "A comprehensive guide to building AI memory graphs in Python.",
        "url": "https://dev.to/alice/mastering-knowledge-graphs",
        "published_at": "2026-10-30T10:00:00Z",
        "tag_list": ["python", "ai", "cognee"],
        "positive_reactions_count": 42,
        "comments_count": 8,
        "reading_time_minutes": 5,
        "user": {
            "name": "Alice Developer",
            "username": "alicedev",
        },
        "body_markdown": "# Mastering Knowledge Graphs with Cognee\nHere is the full content...",
    },
    {
        "id": 102,
        "title": "Top 10 Open Source Projects to Watch in 2026",
        "description": "An overview of impactful developer tooling.",
        "url": "https://dev.to/bob/top-10-open-source-2026",
        "published_at": "2026-10-29T14:30:00Z",
        "tag_list": ["opensource", "webdev"],
        "positive_reactions_count": 85,
        "comments_count": 14,
        "reading_time_minutes": 7,
        "user": {
            "name": "Bob Builder",
            "username": "bobbuilder",
        },
        "body_markdown": "# Top 10 Open Source Projects\nDetailed list of projects...",
    },
]


def test_get_headers_without_key():
    headers = _get_headers(None)
    assert "Accept" in headers
    assert "api-key" not in headers


def test_get_headers_with_key():
    headers = _get_headers("test-devto-key-xyz")
    assert headers.get("api-key") == "test-devto-key-xyz"


def test_format_article_to_document():
    doc = format_article_to_document(SAMPLE_ARTICLES[0])
    assert "# Mastering Knowledge Graphs with Cognee" in doc
    assert "Alice Developer (@alicedev)" in doc
    assert "python, ai, cognee" in doc
    assert "Reactions: 42" in doc
    assert "Reading Time: 5 min" in doc


@patch("requests.get")
def test_fetch_devto_articles(mock_get):
    mock_resp = MagicMock()
    mock_resp.json.return_value = SAMPLE_ARTICLES
    mock_resp.raise_for_status.return_value = None
    mock_get.return_value = mock_resp

    articles = fetch_devto_articles(tag="python", per_page=2)
    assert len(articles) == 2
    assert articles[0]["title"] == "Mastering Knowledge Graphs with Cognee"
    mock_get.assert_called_once()


@patch("requests.get")
def test_fetch_devto_article_by_id(mock_get):
    mock_resp = MagicMock()
    mock_resp.json.return_value = SAMPLE_ARTICLES[0]
    mock_resp.raise_for_status.return_value = None
    mock_get.return_value = mock_resp

    article = fetch_devto_article_by_id(101)
    assert article["id"] == 101
    assert "body_markdown" in article


@patch("requests.get")
def test_fetch_devto_documents(mock_get):
    mock_resp = MagicMock()
    mock_resp.json.return_value = SAMPLE_ARTICLES
    mock_resp.raise_for_status.return_value = None
    mock_get.return_value = mock_resp

    docs = fetch_devto_documents(tag="python", per_page=2, fetch_full_content=False)
    assert len(docs) == 2
    assert "Mastering Knowledge Graphs with Cognee" in docs[0]
    assert "Top 10 Open Source Projects" in docs[1]


@patch("requests.get")
def test_devto_source_generator(mock_get):
    mock_resp = MagicMock()
    mock_resp.json.return_value = SAMPLE_ARTICLES
    mock_resp.raise_for_status.return_value = None
    mock_get.return_value = mock_resp

    try:
        source_resource = devto_source(tag="python", per_page=2)
        data = list(source_resource())
        assert len(data) == 2
        assert "formatted_document" in data[0]
    except ImportError:
        pytest.skip("dlt is not installed, skipping DLT resource test.")
