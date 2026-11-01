"""Tests for the Substack connector."""

import pytest
from unittest.mock import MagicMock
from cognee_community_connector_substack.substack import (
    substack_posts,
    SUBSTACK_SOURCE_NAME,
    DOCUMENT_SOURCE_ATTR,
)


FAKE_POSTS_PAGE_1 = [
    {
        "id": 101,
        "title": "First Post",
        "subtitle": "An intro",
        "slug": "first-post",
        "post_date": "2026-09-01T00:00:00Z",
        "canonical_url": "https://demo.substack.com/p/first-post",
        "description": "Short desc 1",
        "body_text": "Full body text of the first post.",
    },
    {
        "id": 102,
        "title": "Second Post",
        "subtitle": "",
        "slug": "second-post",
        "post_date": "2026-09-15T00:00:00Z",
        "canonical_url": "https://demo.substack.com/p/second-post",
        "description": "Short desc 2",
        "body_text": "Full body text of the second post.",
    },
]


class MockResponse:
    def __init__(self, json_data, status_code=200):
        self._json = json_data
        self.status_code = status_code

    def json(self):
        return self._json

    def raise_for_status(self):
        pass


@pytest.fixture
def mock_requests(monkeypatch):
    from dlt.sources.helpers import requests

    call_count = {"n": 0}

    def mock_get(url, params=None):
        call_count["n"] += 1
        offset = (params or {}).get("offset", 0)
        if offset == 0:
            return MockResponse(FAKE_POSTS_PAGE_1)
        return MockResponse([])

    monkeypatch.setattr(requests, "get", mock_get)


def test_substack_yields_posts(mock_requests):
    """Verify the source yields the expected rows."""
    source = substack_posts(subdomain="demo")
    rows = list(source)
    assert len(rows) == 2
    assert rows[0]["id"] == "substack_101"
    assert rows[0]["title"] == "First Post"
    assert "Full body text of the first post." in rows[0]["content"]
    assert rows[1]["id"] == "substack_102"


def test_substack_missing_subdomain():
    """Should raise when subdomain is empty."""
    with pytest.raises(Exception):
        list(substack_posts(subdomain=""))


def test_substack_document_marker():
    """The source must carry the DOCUMENT_SOURCE_ATTR marker."""
    source = substack_posts(subdomain="demo")
    assert getattr(source, DOCUMENT_SOURCE_ATTR) == SUBSTACK_SOURCE_NAME
