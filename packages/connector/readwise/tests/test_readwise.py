"""Tests for the Readwise connector."""

import pytest
from cognee_community_connector_readwise.readwise import (
    readwise_highlights,
    READWISE_SOURCE_NAME,
    DOCUMENT_SOURCE_ATTR,
)


FAKE_EXPORT = {
    "results": [
        {
            "title": "Sapiens",
            "author": "Yuval Noah Harari",
            "category": "books",
            "source_url": "https://readwise.io/bookreview/sapiens",
            "highlights": [
                {
                    "id": 5001,
                    "text": "History began when humans invented gods.",
                    "note": "Powerful opening line",
                    "location": 42,
                    "highlighted_at": "2026-01-15T10:00:00Z",
                    "tags": [{"name": "philosophy"}],
                },
                {
                    "id": 5002,
                    "text": "Money is the most universal system of mutual trust.",
                    "note": "",
                    "location": 200,
                    "highlighted_at": "2026-01-16T08:00:00Z",
                    "tags": [],
                },
            ],
        },
        {
            "title": "Some Blog Post",
            "author": "Jane Blogger",
            "category": "articles",
            "source_url": "https://example.com/blog",
            "highlights": [
                {
                    "id": 5003,
                    "text": "AI is transforming everything.",
                    "note": "",
                    "location": 1,
                    "highlighted_at": "2026-02-01T12:00:00Z",
                    "tags": [{"name": "AI"}, {"name": "tech"}],
                },
            ],
        },
    ],
    "nextPageCursor": None,
}


class MockResponse:
    def __init__(self, json_data):
        self._json = json_data
        self.status_code = 200

    def json(self):
        return self._json

    def raise_for_status(self):
        pass


@pytest.fixture
def mock_requests(monkeypatch):
    from dlt.sources.helpers import requests

    def mock_get(url, headers=None, params=None):
        return MockResponse(FAKE_EXPORT)

    monkeypatch.setattr(requests, "get", mock_get)


def test_readwise_yields_highlights(mock_requests):
    """Verify the source yields the expected highlight rows."""
    source = readwise_highlights(api_token="dummy")
    rows = list(source)
    assert len(rows) == 3

    assert rows[0]["id"] == "readwise_5001"
    assert "Sapiens" in rows[0]["title"]
    assert "History began" in rows[0]["content"]
    assert "philosophy" in rows[0]["content"]
    assert "Powerful opening line" in rows[0]["content"]

    assert rows[2]["id"] == "readwise_5003"
    assert "AI, tech" in rows[2]["content"]


def test_readwise_missing_token():
    """Should raise when token is missing."""
    with pytest.raises(Exception, match="API token"):
        list(readwise_highlights(api_token=None))


def test_readwise_document_marker():
    """The source must carry the DOCUMENT_SOURCE_ATTR marker."""
    source = readwise_highlights(api_token="dummy")
    assert getattr(source, DOCUMENT_SOURCE_ATTR) == READWISE_SOURCE_NAME
