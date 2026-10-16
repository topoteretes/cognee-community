"""Tests for the Hacker News connector."""

import pytest
from cognee_community_connector_hacker_news.hacker_news import (
    hacker_news_stories,
    HN_SOURCE_NAME,
    DOCUMENT_SOURCE_ATTR,
)


FAKE_IDS = [1001, 1002, 1003]

FAKE_ITEMS = {
    1001: {
        "id": 1001,
        "type": "story",
        "title": "Show HN: My New Project",
        "url": "https://example.com/project",
        "score": 150,
        "by": "alice",
        "time": 1696000000,
        "descendants": 42,
        "text": "",
    },
    1002: {
        "id": 1002,
        "type": "story",
        "title": "Ask HN: Best resources for learning Rust?",
        "url": "",
        "score": 90,
        "by": "bob",
        "time": 1696001000,
        "descendants": 30,
        "text": "Looking for good tutorials and books on Rust.",
    },
    1003: {
        "id": 1003,
        "type": "comment",  # should be skipped
        "text": "Great post!",
        "by": "charlie",
        "time": 1696002000,
    },
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

    def mock_get(url, **kwargs):
        if "topstories" in url:
            return MockResponse(FAKE_IDS)
        for item_id, item in FAKE_ITEMS.items():
            if f"/item/{item_id}.json" in url:
                return MockResponse(item)
        return MockResponse(None)

    monkeypatch.setattr(requests, "get", mock_get)


def test_hn_yields_stories(mock_requests):
    """Verify the source yields only story-type items."""
    source = hacker_news_stories(story_type="top", max_items=10)
    rows = list(source)
    # Should get 2 stories (1003 is a comment, skipped)
    assert len(rows) == 2
    assert rows[0]["id"] == "hn_1001"
    assert rows[0]["title"] == "Show HN: My New Project"
    assert "alice" in rows[0]["content"]
    assert rows[1]["id"] == "hn_1002"
    assert "Looking for good tutorials" in rows[1]["content"]


def test_hn_invalid_story_type():
    """Should raise on invalid story_type."""
    with pytest.raises(Exception, match="story_type"):
        list(hacker_news_stories(story_type="invalid"))


def test_hn_document_marker():
    """The source must carry the DOCUMENT_SOURCE_ATTR marker."""
    source = hacker_news_stories(story_type="top")
    assert getattr(source, DOCUMENT_SOURCE_ATTR) == HN_SOURCE_NAME
