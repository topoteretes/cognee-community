"""Unit tests for the Raindrop.io dlt connector.

Two layers, all runnable in CI without a live Raindrop.io token:

* DB-free tests for bookmark→row flattening, content building, and the
  generic document DataItem tagging (``source="raindrop"``).
* dlt-pipeline tests (mocked httpx client, temp sqlite destination) covering
  the acceptance criteria: re-sync reflects edits, and deleted bookmarks
  drop out of the full-snapshot load (forget-on-delete).
"""

from types import SimpleNamespace
from uuid import NAMESPACE_OID, uuid5

import pytest

from cognee.tasks.ingestion.resolve_dlt_sources import _build_document_data_item

from cognee_community_connector_raindrop.raindrop import (
    RAINDROP_SOURCE_NAME,
    _bookmark_to_row,
    _build_content,
    _iter_bookmarks,
    _paginate,
)

# ---------------------------------------------------------------------------
# Fixtures / fakes
# ---------------------------------------------------------------------------


def _bookmark(bookmark_id, title, link="https://example.com", excerpt="", note="", tags=None, collection_id=1):
    return {
        "id": bookmark_id,
        "title": title,
        "link": link,
        "excerpt": excerpt,
        "note": note,
        "tags": tags or [],
        "collection": {"id": collection_id},
    }


class FakeRaindropClient:
    """Stand-in for httpx.Client backed by in-memory fixtures."""

    def __init__(self, bookmarks):
        self._bookmarks = bookmarks
        self._call_count = 0

    def get(self, path, params=None):
        self._call_count += 1
        params = params or {}

        if path.startswith("/rest/v1/raindrops/"):
            cid = int(path.split("/")[-1])
            if cid == 0:
                # /raindrops/0 returns all bookmarks
                items = list(self._bookmarks)
            else:
                items = [b for b in self._bookmarks if b["collection"]["id"] == cid]
            page = params.get("page", 0)
            per_page = params.get("perpage", 50)
            start = page * per_page
            page_items = items[start : start + per_page]
            return self._json_response({"items": page_items})

        return self._json_response({"items": []})

    def _json_response(self, data):
        return SimpleNamespace(
            json=lambda: data,
            raise_for_status=lambda: None,
            status_code=200,
        )


# ---------------------------------------------------------------------------
# Content building (DB-free)
# ---------------------------------------------------------------------------


def test_build_content_with_all_fields():
    bookmark = _bookmark(1, "Test", excerpt="An excerpt", note="My notes", tags=["python", "ai"])
    content = _build_content(bookmark)
    assert "# Test" in content
    assert "An excerpt" in content
    assert "My notes" in content
    assert "python, ai" in content


def test_build_content_minimal():
    bookmark = _bookmark(1, "Bare")
    content = _build_content(bookmark)
    assert "# Bare" in content
    assert "Notes" not in content
    assert "Tags" not in content


def test_bookmark_to_row_flattens():
    bookmark = _bookmark(42, "My Bookmark", link="https://example.com/doc", excerpt="Excerpt", note="Note", tags=["tag1"])
    row = _bookmark_to_row(bookmark)
    assert row["id"] == "42"
    assert row["url"] == "https://example.com/doc"
    assert row["title"] == "My Bookmark"
    assert "Excerpt" in row["content"]
    assert "Note" in row["content"]
    assert "tag1" in row["content"]


def test_bookmark_to_row_handles_missing_fields():
    bookmark = {"id": 1, "link": "https://x.com"}
    row = _bookmark_to_row(bookmark)
    assert row["id"] == "1"
    assert row["title"] == ""
    assert row["url"] == "https://x.com"


# ---------------------------------------------------------------------------
# Pagination (DB-free)
# ---------------------------------------------------------------------------


def test_paginate_follows_cursor():
    bookmarks = [_bookmark(i, f"Book {i}") for i in range(120)]
    client = FakeRaindropClient(bookmarks)
    result = _paginate(client, "get", "/rest/v1/raindrops/1")
    assert len(result) == 120
    # 120 items at per_page=50 means 3 API calls
    assert client._call_count == 3


def test_paginate_stops_when_empty():
    client = FakeRaindropClient([])
    result = _paginate(client, "get", "/rest/v1/raindrops/1")
    assert len(result) == 0
    assert client._call_count == 1


# ---------------------------------------------------------------------------
# Iteration (DB-free)
# ---------------------------------------------------------------------------


def test_iter_bookmarks_fetches_all_when_no_collection_ids():
    bookmarks = [
        _bookmark(1, "A", collection_id=1),
        _bookmark(2, "B", collection_id=2),
    ]
    client = FakeRaindropClient(bookmarks)
    result = list(_iter_bookmarks(client))
    assert len(result) == 2


def test_iter_bookmarks_respects_collection_filter():
    bookmarks = [
        _bookmark(1, "A", collection_id=1),
        _bookmark(2, "B", collection_id=2),
    ]
    client = FakeRaindropClient(bookmarks)
    result = list(_iter_bookmarks(client, collection_ids=[1]))
    assert len(result) == 1
    assert result[0]["id"] == 1


# ---------------------------------------------------------------------------
# Document DataItem tagging (DB-free)
# ---------------------------------------------------------------------------


def test_build_document_data_item_tags_source():
    row = SimpleNamespace(
        row_data={
            "id": "42",
            "url": "https://example.com",
            "title": "My Bookmark",
            "content": "# My Bookmark\n\nExcerpt",
        },
        content_hash="abc123",
    )
    data_id = uuid5(NAMESPACE_OID, "42")

    item = _build_document_data_item(row, data_id, "raindrop")

    assert item.external_metadata["source"] == "raindrop"
    assert item.external_metadata["url"] == "https://example.com"
    assert item.external_metadata["external_id"] == "42"
    assert item.data_id == data_id
    assert "My Bookmark" in item.data


def test_raindrop_source_declares_document_marker():
    from cognee.tasks.ingestion.dlt_utils import document_source_tag

    from cognee_community_connector_raindrop.raindrop import raindrop_source

    source = raindrop_source(token="test-token")
    assert RAINDROP_SOURCE_NAME == "raindrop"
    assert document_source_tag(source) == "raindrop"


# ---------------------------------------------------------------------------
# dlt pipeline: full-snapshot sync + forget-on-delete
# ---------------------------------------------------------------------------


def _run_sync(dlt, tmp_path, bookmarks, collection_ids=None):
    """Run raindrop_source through a dlt pipeline into a temp sqlite destination."""
    from cognee_community_connector_raindrop.raindrop import raindrop_source

    db_path = (tmp_path / "raindrop.db").as_posix()
    pipeline = dlt.pipeline(
        pipeline_name="raindrop_test",
        destination=dlt.destinations.sqlalchemy(f"sqlite:///{db_path}"),
        dataset_name="raindrop_ds",
        pipelines_dir=str(tmp_path / "state"),
    )
    client = FakeRaindropClient(bookmarks)
    pipeline.run(raindrop_source(client=client, collection_ids=collection_ids))
    return pipeline


def _read_bookmarks(pipeline):
    """Return {id: row-dict} for the raindrop_bookmarks table."""
    with (
        pipeline.sql_client() as client,
        client.execute_query("SELECT id, title, content FROM raindrop_bookmarks") as cursor,
    ):
        rows = cursor.fetchall()
    return {row[0]: {"id": row[0], "title": row[1], "content": row[2]} for row in rows}


@pytest.fixture
def dlt_mod():
    return pytest.importorskip("dlt")


def test_first_sync_loads_bookmarks(dlt_mod, tmp_path):
    bookmarks = [_bookmark(1, "Alpha", excerpt="first sync")]
    pipeline = _run_sync(dlt_mod, tmp_path, bookmarks)

    rows = _read_bookmarks(pipeline)
    assert set(rows) == {"1"}
    assert "first sync" in rows["1"]["content"]


def test_edit_is_reflected_on_resync(dlt_mod, tmp_path):
    _run_sync(dlt_mod, tmp_path, [_bookmark(1, "Alpha", excerpt="v1")])

    pipeline = _run_sync(dlt_mod, tmp_path, [_bookmark(1, "Alpha", excerpt="v2")])

    rows = _read_bookmarks(pipeline)
    assert "v2" in rows["1"]["content"]
    assert "v1" not in rows["1"]["content"]


def test_deleted_bookmark_is_removed_on_resync(dlt_mod, tmp_path):
    _run_sync(dlt_mod, tmp_path, [
        _bookmark(1, "Alpha"),
        _bookmark(2, "Beta"),
    ])

    # Only Beta remains after Alpha is deleted upstream.
    pipeline = _run_sync(dlt_mod, tmp_path, [_bookmark(2, "Beta")])

    rows = _read_bookmarks(pipeline)
    assert "1" not in rows
    assert "2" in rows


def test_multiple_collections_are_fetched_separately(dlt_mod, tmp_path):
    bookmarks = [
        _bookmark(1, "A", collection_id=1),
        _bookmark(2, "B", collection_id=2),
    ]
    # When collection_ids is provided, each collection is fetched separately
    pipeline = _run_sync(dlt_mod, tmp_path, bookmarks, collection_ids=[1, 2])

    rows = _read_bookmarks(pipeline)
    assert set(rows) == {"1", "2"}


def test_error_is_transient_classification():
    import httpx

    from cognee_community_connector_raindrop.raindrop import _is_transient

    assert _is_transient(httpx.ReadTimeout("t")) is True
    assert _is_transient(httpx.ConnectError("conn refused")) is True

    # Simulate a 429 response using httpx.HTTPStatusError
    req = httpx.Request("GET", "https://api.raindrop.io/rest/v1/test")
    resp = httpx.Response(429, request=req)
    assert _is_transient(httpx.HTTPStatusError("rate limited", request=req, response=resp)) is True

    # Simulate a 200 response (not transient)
    resp200 = httpx.Response(200, request=req)
    assert _is_transient(httpx.HTTPStatusError("ok", request=req, response=resp200)) is False
    assert _is_transient(ValueError()) is False
