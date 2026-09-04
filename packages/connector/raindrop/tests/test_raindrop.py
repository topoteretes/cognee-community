"""Unit tests for the Raindrop.io dlt connector.

Three layers, all runnable in CI without a live Raindrop token:

* DB-free tests for row building, page-text extraction, and error
  classification.
* Sync-strategy tests driving ``_sync`` with an in-memory fake client,
  covering the lastUpdate cursor, early paging stop, id-sweep deletions, and
  the opt-in page fetch.
* dlt-pipeline tests (fake client, temp sqlite destination) covering the
  acceptance criteria: first-run backfill, incremental re-sync picks up only
  changes, and deleted objects vanish from staging (forget-on-delete).
"""

from types import SimpleNamespace
from uuid import NAMESPACE_OID, uuid5

import httpx
import pytest

from cognee_community_connector_raindrop.raindrop import (
    RAINDROP_SOURCE_NAME,
    _annotation_rows,
    _bookmark_row,
    _collection_row,
    _extract_page_text,
    _is_transient,
    _parse_ts,
    _sync,
    _tombstone_row,
    raindrop_source,
)

T0 = "2026-09-01T10:00:00.000Z"  # old
T1 = "2026-09-01T12:00:00.000Z"  # at first sync
T2 = "2026-09-01T14:00:00.000Z"  # new change between runs
T3 = "2026-09-01T16:00:00.000Z"  # an even later change


def _collection(cid=1, title="Reading", last_update=T1, description=""):
    return {"_id": cid, "title": title, "description": description, "lastUpdate": last_update}


def _raindrop(
    rid=100,
    title="Example",
    link="https://example.com",
    excerpt="An excerpt",
    tags=(),
    last_update=T1,
    highlights=(),
    collection_id=1,
):
    return {
        "_id": rid,
        "title": title,
        "link": link,
        "excerpt": excerpt,
        "tags": list(tags),
        "collection": {"$ref": "collections", "id": collection_id},
        "lastUpdate": last_update,
        "highlights": list(highlights),
    }


def _highlight(hid=7, text="a quote", note=""):
    return {"_id": hid, "text": text, "note": note, "lastUpdate": T1}


class FakeRaindrop:
    """Stand-in for RaindropClient backed by in-memory fixtures."""

    def __init__(self, collections=(), raindrops=()):
        self.collection_items = list(collections)
        self.raindrop_items = sorted(raindrops, key=lambda r: r["lastUpdate"], reverse=True)
        self.page_text = "PAGE BODY"
        self.fetch_calls: list[str] = []
        self.page_calls: list[int] = []

    def collections(self) -> list[dict]:
        return list(self.collection_items)

    def raindrops(self, collection_id: str = "0", page: int = 0) -> dict:
        self.page_calls.append(page)
        items = self.raindrop_items[page * 50 : (page + 1) * 50]
        return {"items": items, "count": len(self.raindrop_items)}

    def fetch_page(self, url: str) -> str:
        self.fetch_calls.append(url)
        return self.page_text


# ---------------------------------------------------------------------------
# Row building (DB-free)
# ---------------------------------------------------------------------------


def test_collection_row_flattens_collection():
    row = _collection_row(_collection(cid=5, title="Work", description="Work links"))
    assert row["id"] == "5"
    assert row["kind"] == "collection"
    assert row["title"] == "Work"
    assert row["content"] == "Work links"


def test_bookmark_row_combines_excerpt_and_tags():
    row = _bookmark_row(_raindrop(tags=("zeta", "alpha")))
    assert row["id"] == "100"
    assert row["kind"] == "bookmark"
    assert row["url"] == "https://example.com"
    assert row["title"] == "Example"
    assert "An excerpt" in row["content"]
    assert "Tags: alpha, zeta" in row["content"]  # sorted, deterministic


def test_bookmark_row_appends_opt_in_page_text():
    row = _bookmark_row(_raindrop(), page_text="PAGE BODY")
    assert "PAGE BODY" in row["content"]
    assert (
        _bookmark_row(_raindrop())["content"] == _bookmark_row(_raindrop(), page_text="")["content"]
    )


def test_annotation_rows_prefix_ids_and_merge_note():
    rows = _annotation_rows(
        _raindrop(
            highlights=(
                _highlight(hid=7),
                _highlight(hid=8, text="", note="just a note"),
                _highlight(hid=9, text="the quote", note="why it matters"),
            )
        )
    )
    assert [r["id"] for r in rows] == ["hl-7", "hl-8", "hl-9"]
    assert all(r["kind"] == "annotation" for r in rows)
    assert rows[0]["content"] == "a quote"  # text only
    assert rows[1]["content"] == "just a note"  # note only
    assert "the quote" in rows[2]["content"] and "Note: why it matters" in rows[2]["content"]


def test_tombstone_row_marks_deletion():
    tomb = _tombstone_row("100", "bookmark")
    assert tomb["_deleted"] is True
    assert tomb["kind"] == "bookmark"


# ---------------------------------------------------------------------------
# Page extraction (DB-free)
# ---------------------------------------------------------------------------


def test_extract_page_text_pulls_title_and_description():
    html = (
        "<html><head><title>My Page</title>"
        '<meta name="description" content="About my page"></head>'
        "<body><script>alert(1)</script><p>First para</p><p>Second para</p></body></html>"
    )
    text = _extract_page_text(html)
    assert "My Page" in text
    assert "About my page" in text
    assert "First para" in text
    assert "alert" not in text


def test_extract_page_text_handles_empty_and_capped_output():
    assert _extract_page_text("") == ""
    assert _extract_page_text("<p>x</p>" + "<p>" + "y" * 20000 + "</p>").count("y") <= 5000


# ---------------------------------------------------------------------------
# Timestamp / error classification (DB-free)
# ---------------------------------------------------------------------------


def test_parse_ts_handles_both_suffixes():
    assert _parse_ts("2026-09-01T12:00:00.000Z") == _parse_ts("2026-09-01T12:00:00.000+00:00")
    assert _parse_ts("garbage") is None
    assert _parse_ts(None) is None


def test_is_transient_classification():
    def status_error(code):
        req = httpx.Request("GET", "https://api.raindrop.io/rest/v1/raindrops/0")
        resp = httpx.Response(code, request=req)
        return httpx.HTTPStatusError("err", request=req, response=resp)

    assert _is_transient(status_error(429)) is True
    assert _is_transient(status_error(503)) is True
    assert _is_transient(status_error(401)) is False
    assert _is_transient(httpx.ConnectTimeout("t")) is True
    assert _is_transient(ValueError()) is False


# ---------------------------------------------------------------------------
# Sync strategies (pure, fake client)
# ---------------------------------------------------------------------------


def test_first_sync_yields_everything_and_records_cursors():
    client = FakeRaindrop(
        collections=[_collection(cid=1)],
        raindrops=[_raindrop(rid=100, last_update=T1, highlights=(_highlight(hid=7),))],
    )
    state: dict = {}
    rows = list(_sync(client, state))

    kinds = {r["id"]: r["kind"] for r in rows}
    assert kinds == {"1": "collection", "100": "bookmark", "hl-7": "annotation"}
    assert _parse_ts(state["last_update"]) == _parse_ts(T1)
    assert state["bookmark_ids"] == ["100"]
    assert state["collection_ids"] == ["1"]
    assert state["highlight_ids"] == {"100": ["hl-7"]}
    assert client.fetch_calls == []  # page fetch is opt-in


def test_incremental_sync_reemits_only_changes():
    client = FakeRaindrop(raindrops=[_raindrop(rid=100, last_update=T1)])
    state: dict = {}
    list(_sync(client, state))

    # Nothing changed: nothing is re-emitted.
    client.page_calls.clear()
    assert list(_sync(client, state)) == []

    # A new bookmark arrives (and the old one is still listed).
    client.raindrop_items = [
        _raindrop(rid=100, last_update=T1),
        _raindrop(rid=200, title="New", last_update=T2),
    ]
    rows = list(_sync(client, state))
    assert [r["id"] for r in rows] == ["200"]
    assert _parse_ts(state["last_update"]) == _parse_ts(T2)
    assert sorted(state["bookmark_ids"]) == ["100", "200"]


def test_paging_walks_all_pages_for_the_deletion_sweep():
    # 120 raindrops (3 pages), all last-updated at T0; first sync records T0.
    client = FakeRaindrop(raindrops=[_raindrop(rid=i, last_update=T0) for i in range(120)])
    state: dict = {}
    list(_sync(client, state))

    # Second sync: nothing changed — every page is still walked (the deletion
    # sweep must see all live ids) but nothing is re-emitted.
    client.page_calls.clear()
    assert list(_sync(client, state)) == []
    assert sorted(client.page_calls) == [0, 1, 2]
    assert len(state["bookmark_ids"]) == 120


def test_vanished_bookmark_and_annotations_are_tombstoned():
    client = FakeRaindrop(
        raindrops=[
            _raindrop(rid=100, highlights=(_highlight(hid=7), _highlight(hid=8))),
            _raindrop(rid=200),
        ]
    )
    state: dict = {}
    list(_sync(client, state))

    # Bookmark 100 vanished entirely; 200 remains.
    client.raindrop_items = [_raindrop(rid=200)]
    rows = [r for r in _sync(client, state) if r.get("_deleted")]

    tombs = {r["id"]: r["kind"] for r in rows}
    assert tombs == {"100": "bookmark", "hl-7": "annotation", "hl-8": "annotation"}
    assert state["highlight_ids"] == {}
    assert state["bookmark_ids"] == ["200"]


def test_removed_highlight_is_tombstoned_but_bookmark_survives():
    client = FakeRaindrop(
        raindrops=[_raindrop(rid=100, highlights=(_highlight(hid=7), _highlight(hid=8)))]
    )
    state: dict = {}
    list(_sync(client, state))

    # Highlight 8 was removed from the (still alive, since re-fetched) bookmark.
    client.raindrop_items = [_raindrop(rid=100, last_update=T2, highlights=(_highlight(hid=7),))]
    rows = list(_sync(client, state))

    tombs = [r for r in rows if r.get("_deleted")]
    assert [t["id"] for t in tombs] == ["hl-8"]
    # The bookmark and surviving highlight are re-emitted for the update.
    ids = [r["id"] for r in rows if not r.get("_deleted")]
    assert ids == ["100", "hl-7"]


def test_vanished_collection_is_tombstoned():
    client = FakeRaindrop(collections=[_collection(cid=1), _collection(cid=2, title="Later")])
    state: dict = {}
    list(_sync(client, state))

    client.collection_items = [_collection(cid=1)]
    rows = [r for r in _sync(client, state) if r.get("_deleted")]
    assert [(r["id"], r["kind"]) for r in rows] == [("2", "collection")]


def test_opt_in_page_fetch_and_graceful_failure():
    client = FakeRaindrop(raindrops=[_raindrop(rid=100, last_update=T1)])
    list(_sync(client, {}))
    assert client.fetch_calls == []  # default: no page fetching

    # The bookmark changes (bumping lastUpdate past the cursor): with the
    # opt-in on, the linked page is fetched and appended.
    client.fetch_calls.clear()
    client.page_text = "FETCHED TEXT"
    client.raindrop_items = [_raindrop(rid=100, last_update=T2)]
    rows = list(_sync(client, {}, fetch_page_content=True))
    assert client.fetch_calls == ["https://example.com"]
    assert "FETCHED TEXT" in rows[0]["content"]

    # A failing fetch degrades to the excerpt-only document.
    client.fetch_calls.clear()

    def boom(url):
        raise httpx.ConnectTimeout("t")

    client.fetch_page = boom
    client.raindrop_items = [_raindrop(rid=100, title="Retitled", last_update=T3)]
    rows = list(_sync(client, {}, fetch_page_content=True))
    assert len(rows) == 1
    assert "FETCHED TEXT" not in rows[0]["content"]
    assert "An excerpt" in rows[0]["content"]


def test_non_http_bookmark_link_is_not_fetched():
    client = FakeRaindrop(raindrops=[_raindrop(rid=100, link="javascript:void(0)")])
    list(_sync(client, {}, fetch_page_content=True))
    assert client.fetch_calls == []


# ---------------------------------------------------------------------------
# Source wiring (DB-free)
# ---------------------------------------------------------------------------


def test_raindrop_source_declares_document_marker():
    from cognee.tasks.ingestion.dlt_utils import document_source_tag

    source = raindrop_source(token="test-token")
    assert RAINDROP_SOURCE_NAME == "raindrop"
    assert document_source_tag(source) == "raindrop"


def test_raindrop_source_requires_token():
    with pytest.raises(ValueError):
        raindrop_source(token=None, client=None)


def test_document_data_item_tags_source():
    from cognee.tasks.ingestion.resolve_dlt_sources import _build_document_data_item

    row = SimpleNamespace(
        row_data={
            "id": "100",
            "kind": "bookmark",
            "url": "https://example.com",
            "title": "Example",
            "content": "Example\n\nAn excerpt",
            "_deleted": False,
        },
        content_hash="abc123",
    )
    data_id = uuid5(NAMESPACE_OID, "100")

    item = _build_document_data_item(row, data_id, "raindrop")

    assert item.external_metadata["source"] == "raindrop"
    assert item.external_metadata["url"] == "https://example.com"
    assert item.data_id == data_id
    assert item.data.startswith("# Example")


# ---------------------------------------------------------------------------
# dlt pipeline: incremental merge + forget-on-delete (needs dlt)
# ---------------------------------------------------------------------------


@pytest.fixture
def dlt_mod():
    return pytest.importorskip("dlt")


def _make_pipeline(dlt, tmp_path, name):
    db_path = (tmp_path / f"{name}.db").as_posix()
    return dlt.pipeline(
        pipeline_name=name,
        destination=dlt.destinations.sqlalchemy(f"sqlite:///{db_path}"),
        dataset_name="raindrop_ds",
        pipelines_dir=str(tmp_path / "state"),
    )


def _read_rows(pipeline):
    with (
        pipeline.sql_client() as client,
        client.execute_query("SELECT id, kind, title, content FROM raindrop") as cursor,
    ):
        rows = cursor.fetchall()
    return {row[0]: {"kind": row[1], "title": row[2], "content": row[3]} for row in rows}


def test_pipeline_backfill_and_incremental_resync(dlt_mod, tmp_path):
    client = FakeRaindrop(
        collections=[_collection(cid=1)],
        raindrops=[_raindrop(rid=100, title="Old title", last_update=T1)],
    )
    pipeline = _make_pipeline(dlt_mod, tmp_path, "raindrop_resync")
    pipeline.run(raindrop_source(client=client))

    rows = _read_rows(pipeline)
    assert set(rows) == {"1", "100"}
    assert "Old title" in rows["100"]["content"]

    # Incremental run: only the changed bookmark is re-fetched and upserted.
    client.raindrop_items = [
        _raindrop(rid=100, title="New title", last_update=T2),
        _raindrop(rid=200, title="Added", last_update=T2),
    ]
    pipeline.run(raindrop_source(client=client))

    rows = _read_rows(pipeline)
    assert set(rows) == {"1", "100", "200"}
    assert "New title" in rows["100"]["content"]
    assert "Old title" not in rows["100"]["content"]


def test_pipeline_vanished_bookmark_is_removed(dlt_mod, tmp_path):
    client = FakeRaindrop(raindrops=[_raindrop(rid=100), _raindrop(rid=200)])
    pipeline = _make_pipeline(dlt_mod, tmp_path, "raindrop_forget")
    pipeline.run(raindrop_source(client=client))
    assert set(_read_rows(pipeline)) == {"100", "200"}

    # Bookmark 100 vanished; 200 remains (with a newer update so it re-emits).
    client.raindrop_items = [_raindrop(rid=200, last_update=T2)]
    pipeline.run(raindrop_source(client=client))

    rows = _read_rows(pipeline)
    assert "100" not in rows
    assert "200" in rows
