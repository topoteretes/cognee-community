"""Unit tests for the Contentful dlt connector.

Three layers, all runnable in CI without a live Contentful token:

* DB-free tests for field flattening, row building, and error classification.
* Sync-strategy tests driving ``_sync`` with an in-memory fake client,
  covering the updatedAt cursor, the per-kind id sweep, and deletions.
* dlt-pipeline tests (fake client, temp sqlite destination) covering the
  acceptance criteria: first-run backfill, incremental re-sync picks up only
  changes, and deleted objects vanish from staging (forget-on-delete).
"""

from types import SimpleNamespace
from uuid import NAMESPACE_OID, uuid5

import httpx
import pytest

from cognee_community_connector_contentful.contentful import (
    CONTENTFUL_SOURCE_NAME,
    ContentfulClient,
    _asset_row,
    _changed,
    _content_type_row,
    _entry_row,
    _field_text,
    _flatten_fields,
    _is_transient,
    _parse_ts,
    _sync,
    _tombstone_row,
    contentful_source,
)

T0 = "2026-09-01T10:00:00.000Z"  # old
T1 = "2026-09-01T12:00:00.000Z"  # at first sync
T2 = "2026-09-01T14:00:00.000Z"  # new change between runs
T3 = "2026-09-01T16:00:00.000Z"  # an even later change


def _sys(oid, ctype=None, updated=T1):
    sysinfo = {"id": oid, "updatedAt": updated, "type": "Entry"}
    if ctype:
        sysinfo["contentType"] = {"sys": {"type": "Link", "linkType": "ContentType", "id": ctype}}
    return sysinfo


def _content_type(cid="blogPost", name="Blog Post", display="title", updated=T1):
    return {
        "sys": {"id": cid, "updatedAt": updated},
        "name": name,
        "displayField": display,
        "fields": [
            {"id": "title", "type": "Text", "name": "Title"},
            {"id": "body", "type": "RichText", "name": "Body"},
        ],
    }


def _entry(eid="E1", ctype="blogPost", title="Hello", updated=T1, fields=None):
    if fields is None:
        fields = {
            "title": title,
            "body": {
                "nodeType": "document",
                "content": [
                    {
                        "nodeType": "paragraph",
                        "content": [{"nodeType": "text", "value": "Body text"}],
                    }
                ],
            },
        }
    return {"sys": _sys(eid, ctype, updated), "fields": fields}


def _asset(aid="A1", updated=T1, title="Hero image", url="//images.ctf.app/hero.png"):
    return {
        "sys": _sys(aid, updated=updated),
        "fields": {
            "title": title,
            "description": "The hero",
            "file": {"url": url, "contentType": "image/png", "fileName": "hero.png"},
        },
    }


class FakeContentful:
    """Stand-in for ContentfulClient backed by in-memory fixtures."""

    def __init__(self, types=(), entries=(), assets=()):
        self.type_items = list(types)
        self.entry_items = list(entries)
        self.asset_items = list(assets)

    def content_types(self, skip: int = 0) -> dict:
        return self._page(self.type_items, skip)

    def entries(self, skip: int = 0) -> dict:
        return self._page(self.entry_items, skip)

    def assets(self, skip: int = 0) -> dict:
        return self._page(self.asset_items, skip)

    @staticmethod
    def _page(items, skip):
        batch = items[skip : skip + 200]
        return {"items": batch, "total": len(items), "skip": skip}


# ---------------------------------------------------------------------------
# Field flattening / rows (DB-free)
# ---------------------------------------------------------------------------


def test_field_text_handles_scalars_lists_and_rich_text():
    assert _field_text("plain") == "plain"
    assert _field_text(42) == "42"
    assert _field_text(True) == "True"
    assert _field_text(["a", "b"]) == "a\nb"
    rich = {
        "nodeType": "document",
        "content": [
            {
                "nodeType": "paragraph",
                "content": [
                    {"nodeType": "text", "value": "one"},
                    {"nodeType": "text", "value": "two"},
                ],
            }
        ],
    }
    assert _field_text(rich) == "onetwo"


def test_field_text_renders_links_as_placeholders_without_dereferencing():
    link = {"sys": {"type": "Link", "linkType": "Entry", "id": "E9"}}
    assert _field_text(link) == "[Entry: E9]"


def test_flatten_fields_pairs_names_with_text():
    fields = {"title": "Hello", "tags": ["x", "y"], "missing": None}
    text = _flatten_fields(fields)
    assert "title: Hello" in text
    assert "tags:\nx\ny" in text
    assert "missing" not in text


def test_entry_title_uses_content_type_display_field():
    types = {"blogPost": _content_type(cid="blogPost", display="title")}
    entry = _entry(fields={"title": "My Post", "body": "irrelevant"})
    assert _entry_row(entry, types)["title"] == "My Post"


def test_entry_row_without_display_field_falls_back_to_first_text():
    types = {"blogPost": _content_type(cid="blogPost", display=None)}
    entry = _entry(eid="E2", fields={"body": "text body", "title": 7})
    row = _entry_row(entry, types)
    assert row["title"] == "text body"
    assert row["id"] == "E2"


def test_content_type_row_describes_fields():
    row = _content_type_row(_content_type())
    assert row["kind"] == "content_type"
    assert row["id"] == "blogPost"
    assert "title (Text)" in row["content"]
    assert "body (RichText)" in row["content"]


def test_asset_row_normalizes_protocol_relative_url():
    row = _asset_row(_asset())
    assert row["kind"] == "asset"
    assert row["url"] == "https://images.ctf.app/hero.png"
    assert "image/png" in row["content"]
    assert "The hero" in row["content"]


def test_tombstone_row_marks_deletion():
    tomb = _tombstone_row("E1", "entry")
    assert tomb["_deleted"] is True
    assert tomb["kind"] == "entry"


# ---------------------------------------------------------------------------
# Timestamp / error classification (DB-free)
# ---------------------------------------------------------------------------


def test_parse_ts_handles_both_suffixes():
    assert _parse_ts("2026-09-01T12:00:00.000Z") == _parse_ts("2026-09-01T12:00:00.000+00:00")
    assert _parse_ts("garbage") is None
    assert _parse_ts(None) is None


def test_changed_compares_updated_at_to_cursor():
    assert _changed(_entry(), None) is True
    assert _changed(_entry(updated=T2), _parse_ts(T1)) is True
    assert _changed(_entry(updated=T0), _parse_ts(T1)) is False
    assert _changed({"sys": {"id": "x"}}, _parse_ts(T1)) is True  # unparseable → treated as changed


def test_is_transient_classification():
    def status_error(code):
        req = httpx.Request("GET", "https://cdn.contentful.com/spaces/s/entries")
        resp = httpx.Response(code, request=req)
        return httpx.HTTPStatusError("err", request=req, response=resp)

    assert _is_transient(status_error(429)) is True
    assert _is_transient(status_error(503)) is True
    assert _is_transient(status_error(401)) is False
    assert _is_transient(httpx.ReadTimeout("t")) is True
    assert _is_transient(ValueError()) is False


# ---------------------------------------------------------------------------
# Sync strategies (pure, fake client)
# ---------------------------------------------------------------------------


def test_first_sync_yields_everything_and_records_ids():
    client = FakeContentful(
        types=[_content_type()],
        entries=[_entry(eid="E1"), _entry(eid="E2", ctype="blogPost", updated=T0)],
        assets=[_asset(aid="A1")],
    )
    state: dict = {}
    rows = list(_sync(client, state))

    ids = {(r["id"], r["kind"]) for r in rows}
    assert ("blogPost", "content_type") in ids
    assert ("E1", "entry") in ids and ("E2", "entry") in ids
    assert ("A1", "asset") in ids
    assert state["ids"]["entry"] == ["E1", "E2"]
    assert _parse_ts(state["last_update"]) == _parse_ts(T1)


def test_incremental_sync_reemits_only_changes():
    client = FakeContentful(entries=[_entry(eid="E1", updated=T1)])
    state: dict = {}
    list(_sync(client, state))

    # Nothing changed: nothing is re-emitted.
    assert list(_sync(client, state)) == []

    # One entry updated, one new entry appears.
    client.entry_items = [
        _entry(eid="E1", title="Updated", updated=T3),
        _entry(eid="E2", title="New", updated=T2),
    ]
    rows = list(_sync(client, state))
    assert sorted(r["id"] for r in rows) == ["E1", "E2"]
    assert _parse_ts(state["last_update"]) == _parse_ts(T3)


def test_vanished_entry_is_tombstoned():
    client = FakeContentful(entries=[_entry(eid="E1"), _entry(eid="E2")])
    state: dict = {}
    list(_sync(client, state))

    # E1 vanished upstream; E2 was merely updated.
    client.entry_items = [_entry(eid="E2", updated=T2)]
    rows = [r for r in _sync(client, state) if r.get("_deleted")]
    assert [(r["id"], r["kind"]) for r in rows] == [("E1", "entry")]


def test_vanished_asset_and_content_type_are_tombstoned():
    client = FakeContentful(
        types=[_content_type(cid="c1"), _content_type(cid="c2", name="Other")],
        assets=[_asset(aid="A1"), _asset(aid="A2", title="Other")],
    )
    state: dict = {}
    list(_sync(client, state))

    client.type_items = [_content_type(cid="c1")]
    client.asset_items = [_asset(aid="A1", updated=T2)]
    rows = [r for r in _sync(client, state) if r.get("_deleted")]
    assert sorted((r["id"], r["kind"]) for r in rows) == [("A2", "asset"), ("c2", "content_type")]


# ---------------------------------------------------------------------------
# Source wiring (DB-free)
# ---------------------------------------------------------------------------


def test_contentful_source_declares_document_marker():
    from cognee.tasks.ingestion.dlt_utils import document_source_tag

    source = contentful_source(space_id="s", token="t")
    assert CONTENTFUL_SOURCE_NAME == "contentful"
    assert document_source_tag(source) == "contentful"


def test_contentful_source_requires_space_and_token():
    with pytest.raises(ValueError):
        contentful_source(space_id=None, token=None, client=None)


def test_document_data_item_tags_source():
    from cognee.tasks.ingestion.resolve_dlt_sources import _build_document_data_item

    row = SimpleNamespace(
        row_data={
            "id": "E1",
            "kind": "entry",
            "url": "",
            "title": "Hello",
            "content": "title: Hello",
            "_deleted": False,
        },
        content_hash="abc123",
    )
    data_id = uuid5(NAMESPACE_OID, "E1")

    item = _build_document_data_item(row, data_id, "contentful")

    assert item.external_metadata["source"] == "contentful"
    assert item.external_metadata["external_id"] == "E1"
    assert item.data_id == data_id
    assert item.data.startswith("# Hello")


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
        dataset_name="contentful_ds",
        pipelines_dir=str(tmp_path / "state"),
    )


def _read_rows(pipeline):
    with (
        pipeline.sql_client() as client,
        client.execute_query("SELECT id, kind, title, content FROM contentful") as cursor,
    ):
        rows = cursor.fetchall()
    return {row[0]: {"kind": row[1], "title": row[2], "content": row[3]} for row in rows}


def test_pipeline_backfill_and_incremental_resync(dlt_mod, tmp_path):
    client = FakeContentful(
        types=[_content_type()],
        entries=[_entry(eid="E1", title="Hello")],
    )
    pipeline = _make_pipeline(dlt_mod, tmp_path, "contentful_resync")
    pipeline.run(contentful_source(space_id="s", token="t", client=client))

    rows = _read_rows(pipeline)
    assert set(rows) == {"blogPost", "E1"}
    assert rows["E1"]["title"] == "Hello"

    # Incremental run: the entry is updated in place via merge upsert.
    client.entry_items = [_entry(eid="E1", title="Updated", updated=T2)]
    pipeline.run(contentful_source(space_id="s", token="t", client=client))

    rows = _read_rows(pipeline)
    assert set(rows) == {"blogPost", "E1"}
    assert rows["E1"]["title"] == "Updated"


def test_pipeline_vanished_entry_is_removed(dlt_mod, tmp_path):
    client = FakeContentful(entries=[_entry(eid="E1"), _entry(eid="E2")])
    pipeline = _make_pipeline(dlt_mod, tmp_path, "contentful_forget")
    pipeline.run(contentful_source(space_id="s", token="t", client=client))
    assert set(_read_rows(pipeline)) == {"E1", "E2"}

    # E1 vanished upstream; E2 was merely updated.
    client.entry_items = [_entry(eid="E2", updated=T2)]
    pipeline.run(contentful_source(space_id="s", token="t", client=client))

    rows = _read_rows(pipeline)
    assert "E1" not in rows
    assert "E2" in rows


def test_client_auth_and_url_shape():
    client = ContentfulClient("space1", "tok", environment="staging")
    assert client._base.endswith("/spaces/space1/environments/staging")

    captured = {}

    def fake_get(url, params, headers, timeout):
        captured.update(url=url, params=params, headers=headers)
        return httpx.Response(
            200, json={"items": [], "total": 0}, request=httpx.Request("GET", url)
        )

    import httpx as httpx_mod

    original = httpx_mod.get
    httpx_mod.get = fake_get
    try:
        payload = client.entries(skip=0)
    finally:
        httpx_mod.get = original
    assert payload == {"items": [], "total": 0}
    assert captured["headers"]["Authorization"] == "Bearer tok"
    assert captured["params"]["order"] == "-sys.updatedAt"
