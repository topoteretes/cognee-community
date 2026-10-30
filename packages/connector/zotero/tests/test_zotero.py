"""Tests for the Zotero connector."""

import pytest
from cognee_community_connector_zotero.zotero import (
    zotero_items,
    ZOTERO_SOURCE_NAME,
    DOCUMENT_SOURCE_ATTR,
)


FAKE_ITEMS = [
    {
        "key": "ABC123",
        "data": {
            "key": "ABC123",
            "itemType": "journalArticle",
            "title": "Deep Learning for Protein Folding",
            "creators": [
                {"firstName": "Jane", "lastName": "Doe"},
                {"firstName": "John", "lastName": "Smith"},
            ],
            "date": "2025-06-15",
            "abstractNote": "We present a novel approach to protein folding.",
            "url": "https://example.com/paper1",
            "tags": [{"tag": "ML"}, {"tag": "biology"}],
            "publicationTitle": "Nature",
            "DOI": "10.1234/example",
        },
    },
    {
        "key": "DEF456",
        "data": {
            "key": "DEF456",
            "itemType": "book",
            "title": "Introduction to Algorithms",
            "creators": [{"firstName": "Thomas", "lastName": "Cormen"}],
            "date": "2009",
            "abstractNote": "",
            "url": "",
            "tags": [{"tag": "CS"}],
            "bookTitle": "",
            "DOI": "",
        },
    },
]


class MockResponse:
    def __init__(self, json_data, total=None):
        self._json = json_data
        self.headers = {"Total-Results": str(total if total is not None else len(json_data))}
        self.status_code = 200

    def json(self):
        return self._json

    def raise_for_status(self):
        pass


@pytest.fixture
def mock_requests(monkeypatch):
    from dlt.sources.helpers import requests

    def mock_get(url, headers=None, params=None):
        start = (params or {}).get("start", 0)
        if start == 0:
            return MockResponse(FAKE_ITEMS, total=2)
        return MockResponse([], total=2)

    monkeypatch.setattr(requests, "get", mock_get)


def test_zotero_yields_items(mock_requests):
    """Verify the source yields the expected rows."""
    source = zotero_items(api_key="dummy", user_id="12345")
    rows = list(source)
    assert len(rows) == 2

    assert rows[0]["id"] == "zotero_ABC123"
    assert rows[0]["title"] == "Deep Learning for Protein Folding"
    assert "Jane Doe" in rows[0]["content"]
    assert "10.1234/example" in rows[0]["content"]

    assert rows[1]["id"] == "zotero_DEF456"
    assert rows[1]["title"] == "Introduction to Algorithms"


def test_zotero_missing_api_key():
    """Should raise when api_key is missing."""
    with pytest.raises(Exception, match="API key"):
        list(zotero_items(api_key=None, user_id="12345"))


def test_zotero_missing_user_id():
    """Should raise when user_id is missing."""
    with pytest.raises(Exception, match="user_id"):
        list(zotero_items(api_key="dummy", user_id=None))


def test_zotero_document_marker():
    """The source must carry the DOCUMENT_SOURCE_ATTR marker."""
    source = zotero_items(api_key="dummy", user_id="12345")
    assert getattr(source, DOCUMENT_SOURCE_ATTR) == ZOTERO_SOURCE_NAME
