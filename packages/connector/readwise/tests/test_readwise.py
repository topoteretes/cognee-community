"""Unit tests for the Readwise dlt connector.

Runnable in CI without a Readwise token or network:

* DB-free tests for row building (highlight / note / tombstone), paging, retry
  handling, the incremental cursor and the document tagging.
* dlt-pipeline tests (fake HTTP client, temp sqlite destination) covering the
  acceptance criteria: incremental re-sync picks up only changes, and deleted
  highlights/books drop out of the merged table (forget-on-delete).
"""

import dlt
import httpx
import pytest
from cognee.tasks.ingestion.dlt_utils import DOCUMENT_SOURCE_ATTR
from dlt.pipeline.exceptions import PipelineStepFailed

from cognee_community_connector_readwise import readwise as rw
from cognee_community_connector_readwise.readwise import (
    READWISE_SOURCE_NAME,
    _book_to_rows,
    _iter_books,
    _request,
    readwise_source,
)

# ---------------------------------------------------------------------------
# Fakes / fixtures
# ---------------------------------------------------------------------------


def _highlight(hid, text="a highlight", note=None, tags=None, deleted=False):
    return {
        "id": hid,
        "is_deleted": deleted,
        "text": text,
        "note": note,
        "tags": [{"id": 1, "name": t} for t in tags or []],
        "readwise_url": f"https://readwise.io/open/{hid}",
    }


def _book(bid, highlights, title="Deep Work", author="Cal Newport", **extra):
    book = {
        "user_book_id": bid,
        "is_deleted": False,
        "title": title,
        "readable_title": title,
        "author": author,
        "category": "books",
        "source_url": "",
        "document_note": "",
        "summary": "",
        "book_tags": [],
        "readwise_url": f"https://readwise.io/bookreview/{bid}",
        "highlights": highlights,
    }
    book.update(extra)
    return book


class FakeResponse:
    def __init__(self, status_code=200, payload=None, headers=None):
        self.status_code = status_code
        self._payload = payload or {}
        self.headers = headers or {}

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise httpx.HTTPStatusError(
                "error", request=httpx.Request("GET", rw.READWISE_EXPORT_URL), response=self
            )


class FakeReadwise:
    """In-memory stand-in for httpx.Client that mimics the export endpoint.

    ``pages`` maps a pageCursor (None = first page) to a (results, next) tuple.
    ``calls`` records every request's params so tests can assert on the cursor.
    """

    def __init__(self, pages=None, results=None):
        if results is not None:
            pages = {None: (results, None)}
        self.pages = pages
        self.calls = []

    def get(self, url, params=None):
        assert url == rw.READWISE_EXPORT_URL
        self.calls.append(dict(params or {}))
        results, nxt = self.pages[(params or {}).get("pageCursor")]
        payload = {"count": len(results), "nextPageCursor": nxt, "results": results}
        return FakeResponse(payload=payload)


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    monkeypatch.setattr(rw.time, "sleep", lambda _s: None)


# ---------------------------------------------------------------------------
# Row building (DB-free)
# ---------------------------------------------------------------------------


def test_highlight_row_contains_text_note_tags_and_provenance():
    book = _book(1, [_highlight(10, "Focus is a skill.", note="so true", tags=["focus"])])
    book["source_url"] = "https://example.com/deep-work"
    (row,) = list(_book_to_rows(book))
    assert row["id"] == "highlight:10"
    assert row["_deleted"] is False
    assert row["title"] == "Deep Work - Cal Newport"
    assert row["url"] == "https://readwise.io/open/10"
    for expected in ("Focus is a skill.", "My note: so true", "Tags: focus", "example.com"):
        assert expected in row["content"]


def test_row_does_not_include_volatile_metadata():
    row = next(iter(_book_to_rows(_book(1, [_highlight(10)]))))
    assert set(row) == {"id", "title", "content", "url", "_deleted"}


def test_deleted_highlight_becomes_tombstone():
    rows = list(_book_to_rows(_book(1, [_highlight(10, deleted=True), _highlight(11)])))
    assert rows[0] == {"id": "highlight:10", "_deleted": True}
    assert rows[1]["id"] == "highlight:11"


def test_deleted_book_tombstones_all_highlights_and_note():
    book = _book(1, [_highlight(10), _highlight(11)], is_deleted=True)
    rows = list(_book_to_rows(book))
    assert {r["id"] for r in rows} == {"highlight:10", "highlight:11", "note:1"}
    assert all(r["_deleted"] for r in rows)


def test_book_note_becomes_its_own_row():
    book = _book(1, [], document_note="Read this twice.")
    (row,) = list(_book_to_rows(book))
    assert row["id"] == "note:1"
    assert "Read this twice." in row["content"]


def test_book_without_note_or_highlights_yields_nothing():
    assert list(_book_to_rows(_book(1, []))) == []


# ---------------------------------------------------------------------------
# Paging, params, retry
# ---------------------------------------------------------------------------


def test_iter_books_follows_page_cursor_and_sends_include_deleted():
    client = FakeReadwise(
        pages={None: ([_book(1, [])], "c2"), "c2": ([_book(2, [])], None)},
    )
    books = list(_iter_books(client, None, None))
    assert [b["user_book_id"] for b in books] == [1, 2]
    assert client.calls[0] == {"includeDeleted": "true"}
    assert client.calls[1] == {"includeDeleted": "true", "pageCursor": "c2"}


def test_iter_books_passes_updated_after_and_ids():
    client = FakeReadwise(results=[])
    list(_iter_books(client, "2024-01-01T00:00:00Z", "1,2"))
    assert client.calls[0]["updatedAfter"] == "2024-01-01T00:00:00Z"
    assert client.calls[0]["ids"] == "1,2"


def test_iter_books_stops_on_repeated_cursor():
    client = FakeReadwise(pages={None: ([], "loop"), "loop": ([], "loop")})
    assert list(_iter_books(client, None, None)) == []
    assert len(client.calls) == 2


class _FlakyClient:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = 0

    def get(self, url, params=None):
        self.calls += 1
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def test_request_retries_rate_limit_then_succeeds():
    client = _FlakyClient(
        [FakeResponse(429, headers={"Retry-After": "1"}), FakeResponse(200, {"results": []})]
    )
    assert _request(client, {}) == {"results": []}
    assert client.calls == 2


def test_request_retries_network_error():
    client = _FlakyClient([httpx.ConnectError("boom"), FakeResponse(200, {"results": [1]})])
    assert _request(client, {}) == {"results": [1]}


def test_request_gives_up_after_retry_budget():
    client = _FlakyClient([FakeResponse(503)] * rw._MAX_RETRIES)
    with pytest.raises(httpx.HTTPStatusError):
        _request(client, {})
    assert client.calls == rw._MAX_RETRIES


def test_request_bad_token_raises_permission_error_without_retry():
    client = _FlakyClient([FakeResponse(401)])
    with pytest.raises(PermissionError):
        _request(client, {})
    assert client.calls == 1


# ---------------------------------------------------------------------------
# Source configuration
# ---------------------------------------------------------------------------


def test_source_is_tagged_as_document_source_and_configured_for_merge():
    source = readwise_source(client=FakeReadwise(results=[]))
    assert getattr(source, DOCUMENT_SOURCE_ATTR) == READWISE_SOURCE_NAME
    assert source.name == rw.READWISE_TABLE_NAME
    assert source.write_disposition == "merge"
    assert source._hints["primary_key"] == "id"
    assert source._hints["columns"]["_deleted"]["hard_delete"] is True


def test_source_requires_token_when_no_client(monkeypatch):
    monkeypatch.delenv("READWISE_TOKEN", raising=False)
    with pytest.raises(ValueError, match="READWISE_TOKEN"):
        readwise_source()


def test_source_reads_token_from_env(monkeypatch):
    monkeypatch.setenv("READWISE_TOKEN", "abc")
    assert readwise_source() is not None


# ---------------------------------------------------------------------------
# dlt pipeline: incremental cursor + forget-on-delete (temp sqlite destination)
# ---------------------------------------------------------------------------


def _pipeline(tmp_path):
    return dlt.pipeline(
        pipeline_name="readwise_test",
        destination=dlt.destinations.sqlalchemy(f"sqlite:///{tmp_path / 'rw.db'}"),
        dataset_name="readwise",
        pipelines_dir=str(tmp_path / "pipelines"),
    )


def _run(tmp_path, source):
    pipeline = _pipeline(tmp_path)
    pipeline.run(source)
    return pipeline


def _ids(pipeline):
    query = f"SELECT id FROM {rw.READWISE_TABLE_NAME} ORDER BY id"
    with pipeline.sql_client() as sql, sql.execute_query(query) as cur:
        return [r[0] for r in cur.fetchall()]


def _cursor(pipeline):
    """The saved incremental cursor, wherever dlt sectioned the resource state."""
    found = [
        res[rw.READWISE_TABLE_NAME].get("updated_after")
        for src in pipeline.state["sources"].values()
        for res in [src.get("resources", {})]
        if rw.READWISE_TABLE_NAME in res
    ]
    assert len(found) == 1
    return found[0]


def test_first_run_backfills_everything_and_saves_cursor(tmp_path):
    client = FakeReadwise(results=[_book(1, [_highlight(10), _highlight(11)])])
    pipeline = _run(tmp_path, readwise_source(client=client))
    assert _ids(pipeline) == ["highlight:10", "highlight:11"]
    assert "updatedAfter" not in client.calls[0]
    assert _cursor(pipeline).endswith("Z")


def test_second_run_sends_cursor_and_only_applies_the_delta(tmp_path):
    first = FakeReadwise(results=[_book(1, [_highlight(10, "old"), _highlight(11, "keep")])])
    pipeline = _run(tmp_path, readwise_source(client=first))
    cursor = _cursor(pipeline)

    # Readwise returns ONLY the changed highlight (10 edited) plus a new one (12).
    second = FakeReadwise(results=[_book(1, [_highlight(10, "edited"), _highlight(12, "new")])])
    pipeline = _run(tmp_path, readwise_source(client=second))

    assert second.calls[0]["updatedAfter"] == cursor
    # 11 was not in the delta but must NOT be forgotten.
    assert _ids(pipeline) == ["highlight:10", "highlight:11", "highlight:12"]
    query = f"SELECT content FROM {rw.READWISE_TABLE_NAME} WHERE id = 'highlight:10'"
    with pipeline.sql_client() as sql, sql.execute_query(query) as cur:
        assert "edited" in cur.fetchone()[0]


def test_deleted_highlight_is_removed_on_next_sync(tmp_path):
    first = FakeReadwise(results=[_book(1, [_highlight(10), _highlight(11)])])
    _run(tmp_path, readwise_source(client=first))

    second = FakeReadwise(results=[_book(1, [_highlight(10, deleted=True)])])
    pipeline = _run(tmp_path, readwise_source(client=second))
    assert _ids(pipeline) == ["highlight:11"]


def test_deleted_book_is_removed_on_next_sync(tmp_path):
    first = FakeReadwise(
        results=[_book(1, [_highlight(10)], document_note="n"), _book(2, [_highlight(20)])]
    )
    _run(tmp_path, readwise_source(client=first))

    second = FakeReadwise(results=[_book(1, [_highlight(10)], is_deleted=True)])
    pipeline = _run(tmp_path, readwise_source(client=second))
    assert _ids(pipeline) == ["highlight:20"]


def test_failed_run_does_not_advance_cursor(tmp_path):
    pipeline = _run(
        tmp_path, readwise_source(client=FakeReadwise(results=[_book(1, [_highlight(10)])]))
    )
    cursor = _cursor(pipeline)

    broken = _FlakyClient([FakeResponse(400)])
    with pytest.raises(PipelineStepFailed):
        _run(tmp_path, readwise_source(client=broken))

    pipeline = _pipeline(tmp_path)
    assert _cursor(pipeline) == cursor
    assert _ids(pipeline) == ["highlight:10"]


def test_categories_filter_skips_other_categories(tmp_path):
    client = FakeReadwise(
        results=[
            _book(1, [_highlight(10)], category="books"),
            _book(2, [_highlight(20)], category="tweets"),
        ]
    )
    pipeline = _run(tmp_path, readwise_source(client=client, categories=["Books"]))
    assert _ids(pipeline) == ["highlight:10"]


def test_book_ids_are_sent_to_the_api(tmp_path):
    client = FakeReadwise(results=[])
    _run(tmp_path, readwise_source(client=client, book_ids=[3, 4]))
    assert client.calls[0]["ids"] == "3,4"


def test_explicit_updated_after_is_used_on_first_run(tmp_path):
    client = FakeReadwise(results=[])
    _run(tmp_path, readwise_source(client=client, updated_after="2024-05-01T00:00:00Z"))
    assert client.calls[0]["updatedAfter"] == "2024-05-01T00:00:00Z"
