"""Unit tests for the Pocket connector for cognee."""

import pytest
from unittest.mock import MagicMock

from cognee_community_connector_pocket import (
    POCKET_ARTICLES_TABLE,
    POCKET_SOURCE_NAME,
    iter_pocket_api_articles,
    iter_pocket_export_articles,
    pocket_export_source,
    pocket_source,
)

try:
    from cognee.tasks.ingestion.dlt_utils import DOCUMENT_SOURCE_ATTR
except ImportError:
    DOCUMENT_SOURCE_ATTR = "cognee_document_source"


MOCK_POCKET_API_DATA = {
    "status": 1,
    "list": {
        "101": {
            "item_id": "101",
            "resolved_title": "Understanding Vector Databases",
            "resolved_url": "https://example.com/vector-db",
            "excerpt": "An in-depth guide to HNSW indexing.",
            "authors": {"1": {"name": "Dr. Smith"}},
            "tags": {"vectors": {}, "ai": {}},
            "time_added": "1730500000",
            "status": "0",  # Unread
            "word_count": "2400",
        },
        "102": {
            "item_id": "102",
            "resolved_title": "Archived Article",
            "resolved_url": "https://example.com/archived",
            "excerpt": "Historical data.",
            "status": "1",  # Archived
        },
        "103": {
            "item_id": "103",
            "status": "2",  # Deleted tombstone
        },
    },
}

SAMPLE_HTML_EXPORT = """<!DOCTYPE html>
<html>
<head><title>Pocket Export</title></head>
<body>
<h1>Unread</h1>
<ul>
    <li><a href="https://example.com/item1" time_added="1730501000" tags="tech,ai">Item 1 Title</a></li>
    <li><a href="https://example.com/item2" time_added="1730502000">Item 2 Title</a></li>
</ul>
</body>
</html>
"""


def test_iter_api_articles_extracts_fields_and_tombstones():
    client = lambda: MOCK_POCKET_API_DATA
    records = list(iter_pocket_api_articles(client=client))

    assert len(records) == 3

    item1 = next(r for r in records if r["id"] == "pocket:101")
    assert item1["item_id"] == "101"
    assert item1["title"] == "Article: Understanding Vector Databases"
    assert item1["url"] == "https://example.com/vector-db"
    assert item1["status"] == "Unread"
    assert "Dr. Smith" in item1["authors"]
    assert "vectors" in item1["tags"]
    assert item1["word_count"] == 2400
    assert item1["_deleted"] is False
    assert "An in-depth guide" in item1["content"]

    item2 = next(r for r in records if r["id"] == "pocket:102")
    assert item2["status"] == "Archived"
    assert item2["_deleted"] is False

    item3 = next(r for r in records if r["id"] == "pocket:103")
    assert item3["_deleted"] is True


def test_iter_export_articles_parses_html(tmp_path):
    export_file = tmp_path / "ril_export.html"
    export_file.write_text(SAMPLE_HTML_EXPORT, encoding="utf-8")

    records = list(iter_pocket_export_articles(export_file))
    assert len(records) == 2

    assert records[0]["title"] == "Article: Item 1 Title"
    assert records[0]["url"] == "https://example.com/item1"
    assert records[0]["tags"] == "tech,ai"
    assert records[0]["_deleted"] is False

    assert records[1]["title"] == "Article: Item 2 Title"
    assert records[1]["url"] == "https://example.com/item2"
    assert records[1]["tags"] == ""
    assert records[1]["_deleted"] is False


def test_pocket_source_requires_dlt():
    import builtins
    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name == "dlt":
            raise ImportError("no dlt")
        return real_import(name, *args, **kwargs)

    client = lambda: MOCK_POCKET_API_DATA
    try:
        builtins.__import__ = fake_import
        with pytest.raises(ImportError, match=r"cognee\[dlt\]"):
            pocket_source(client=client)
    finally:
        builtins.__import__ = real_import


def test_pocket_source_declarations():
    pytest.importorskip("dlt")
    client = lambda: MOCK_POCKET_API_DATA
    resource = pocket_source(client=client)

    assert resource.name == POCKET_ARTICLES_TABLE
    assert getattr(resource, DOCUMENT_SOURCE_ATTR) == POCKET_SOURCE_NAME
    assert resource.write_disposition == "merge"
    assert resource.primary_key == ["id"] or resource.primary_key == "id"


def test_empty_api_response_handling():
    assert list(iter_pocket_api_articles(client=lambda: {"status": 1, "list": {}})) == []
    assert list(iter_pocket_api_articles(client=lambda: {"status": 1, "list": []})) == []
