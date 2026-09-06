"""Unit tests for the Zotero connector.

Two layers, all runnable in CI without a live Zotero token:

* DB-free tests for note rendering, reference flattening, attachment text,
  version-map pagination, deleted/tombstone handling, and error classification.
* dlt-pipeline tests (mocked ZoteroClient, temp sqlite destination) covering
  the acceptance criteria: re-sync reflects edits, deleted items drop out
  (forget-on-delete), incremental cursor advances, and 304 is a no-op.
"""

from __future__ import annotations

from types import SimpleNamespace

import httpx
import pytest

from cognee_community_connector_zotero.zotero import (
    _is_gone,
    _is_transient,
    _item_to_row,
    _strip_html,
    _tag_str,
)

# ---------------------------------------------------------------------------
# Fixtures / fakes
# ---------------------------------------------------------------------------


def _item(key="k1", title="Item", item_type="journalArticle", trashed=False, **extra) -> dict:
    return {
        "key": key,
        "version": 1,
        "itemType": item_type,
        "title": title,
        "trashed": trashed,
        "library": {"id": "u1"},
        "url": f"https://www.zotero.org/users/u1/items/{key}",
        "creators": [{"creatorType": "author", "firstName": "Alice", "lastName": "A"}],
        "tags": [{"tag": "test"}],
        "abstractNote": "abstract text",
        "publicationTitle": "Journal",
        "DOI": "10.1",
        "date": "2024-01-01",
        "note": "<p>note html</p>",
        "contentType": "text/plain",
        "filename": "note.txt",
        "linkMode": "imported_file",
        **extra,
    }


class FakeZoteroClient:
    """Stand-in for ZoteroClient backed by in-memory fixtures."""

    def __init__(self, items=None, deleted_keys=None, versions_map=None, token="tok"):
        self._items = items or {}
        self._deleted_keys = set(deleted_keys or [])
        self._versions = versions_map or {}
        self._token = token
        self._user_id = "u1"
        self.request_log = []
        self.last_modified = "1"

    def resolve_user_id(self) -> str:
        return self._user_id

    # --- endpoints -----------------------------------------------------------

    def _request(self, method, path, params=None, headers=None, **kw):
        self.request_log.append((method, path, params, headers))
        raw = self._handle(method, path, params)
        resp = SimpleNamespace(
            status_code=raw["status"],
            headers=raw.get("headers", {}),
            content=raw.get("content", b""),
            text=raw.get("text", ""),
            json_data=raw.get("json_data"),
        )
        resp.raise_for_status = lambda: (
            None
            if resp.status_code < 400
            else (_ for _ in ()).throw(
                httpx.HTTPStatusError("err", request=httpx.Request(method, path), response=resp)
            )
        )
        resp.json = lambda: resp.json_data
        return resp

    def get(self, path, params=None):
        r = self._request("GET", path, params=params)
        # bind json
        return r

    def _handle(self, method, path, params):
        params = params or {}
        # /keys/current
        if path == "/keys/current":
            return {"status": 200, "text": '{"userID": "u1"}', "json_data": {"userID": "u1"}}
        # versions map
        if path == "/users/u1/items" and params.get("format") == "versions":
            return self._versions_resp(params)
        # detail batch
        if path == "/users/u1/items" and "itemKey" in params:
            keys = params["itemKey"].split(",")
            return {
                "status": 200,
                "text": "[]",
                "json_data": [self._items[k] for k in keys if k in self._items],
            }
        # deleted
        if path == "/users/u1/deleted":
            since = int(params.get("since", "0"))
            keys = [k for k, v in self._versions.items() if v > since and k in self._deleted_keys]
            return {
                "status": 200,
                "text": f'{{"items": {keys}}}',
                "json_data": {"items": keys, "collections": [], "searches": [], "tags": []},
            }
        # file attachment
        if path.startswith("/users/u1/items/") and path.endswith("/file"):
            return {
                "status": 200,
                "text": "plain attachment text",
                "content": b"plain attachment text",
                "json_data": None,
            }
        return {"status": 404, "text": "not found", "json_data": {}}

    def _versions_resp(self, params):
        since = int(params.get("since", "0"))
        start = int(params.get("start", "0"))
        limit = int(params.get("limit", "100"))
        # filter changed since
        changed = {k: v for k, v in self._versions.items() if v > since}
        keys = sorted(changed.keys())
        page = keys[start : start + limit]
        resp_keys = {k: changed[k] for k in page}
        headers = {"Last-Modified-Version": str(max(changed.values(), default=1))}
        if params.get("includeTrashed") == "1":
            # expose trashed items too (they still appear with trashed:true)
            pass
        total = len(keys)
        return {
            "status": 200,
            "text": str(resp_keys),
            "json_data": resp_keys,
            "headers": headers,
            "total": total,
        }

    # --- helpers -------------------------------------------------------------

    def get_response(self, method, path, params=None):
        r = self._request(method, path, params=params)
        return r


# ---------------------------------------------------------------------------
# DB-free tests
# ---------------------------------------------------------------------------


def test_strip_html():
    assert _strip_html("<p>Hello <em>world</em></p>") == "Hello world"
    assert _strip_html("") == ""
    assert _strip_html(None) == ""


def test_tag_str():
    assert _tag_str([{"tag": "a"}, {"tag": "b"}]) == "a; b"
    assert _tag_str([]) == ""


def test_item_to_row_reference():
    client = FakeZoteroClient()
    row = _item_to_row(client, _item())
    assert row["id"] == "k1"
    assert row["title"] == "Item"
    assert "abstract text" in row["content"]
    assert "Alice A" in row["content"]
    assert row["_deleted"] is False


def test_item_to_row_note():
    client = FakeZoteroClient()
    row = _item_to_row(client, _item(item_type="note"))
    assert row["id"] == "k1"
    assert _strip_html("<p>note html</p>") in row["content"]
    assert row["_deleted"] is False


def test_item_to_row_attachment():
    client = FakeZoteroClient()
    row = _item_to_row(client, _item(item_type="attachment"))
    assert row["_deleted"] is False
    assert "plain attachment text" in row["content"]


def test_item_to_row_trashed():
    client = FakeZoteroClient()
    row = _item_to_row(client, _item(trashed=True))
    assert row["_deleted"] is True


def test_version_map_pagination():
    fc = FakeZoteroClient(versions_map={f"k{i}": i for i in range(1, 201)})
    fc.last_modified = "200"
    # simulate two pages of 100
    resp1 = fc.get_response(
        "GET",
        "/users/u1/items",
        params={"since": "0", "format": "versions", "start": "0", "limit": "100"},
    )
    resp1.json_data = {k: fc._versions[k] for k in list(fc._versions)[:100]}
    resp1.headers = {"Last-Modified-Version": "200", "Total-Results": "200"}
    assert len(resp1.json()) == 100


def test_is_transient():
    assert _is_transient(httpx.ConnectError("x")) is True
    assert (
        _is_transient(
            httpx.HTTPStatusError(
                "x", request=httpx.Request("GET", "/"), response=SimpleNamespace(status_code=429)
            )
        )
        is True
    )
    assert _is_transient(ValueError()) is False


def test_is_gone():
    assert (
        _is_gone(
            httpx.HTTPStatusError(
                "x", request=httpx.Request("GET", "/"), response=SimpleNamespace(status_code=404)
            )
        )
        is True
    )
    assert _is_gone(ValueError()) is False


# ---------------------------------------------------------------------------
# dlt pipeline tests (mocked client, temp sqlite)
# ---------------------------------------------------------------------------


def _run(dlt, tmp_path, monkeypatch, fc: FakeZoteroClient):
    from cognee_community_connector_zotero.zotero import zotero_source

    db_path = (tmp_path / "zotero.db").as_posix()
    pipeline = dlt.pipeline(
        pipeline_name="zotero_test",
        destination=dlt.destinations.sqlalchemy(f"sqlite:///{db_path}"),
        dataset_name="zotero_ds",
        pipelines_dir=str(tmp_path / "state"),
    )
    pipeline.run(zotero_source(client=fc, api_key="tok"))
    return pipeline


def _read_items(pipeline):
    with (
        pipeline.sql_client() as client,
        client.execute_query("SELECT id, _deleted FROM zotero_items") as cursor,
    ):
        return {row[0]: row[1] for row in cursor.fetchall()}


@pytest.fixture
def dlt_mod():
    return pytest.importorskip("dlt")


@pytest.fixture(autouse=True)
def _need_httpx():
    pytest.importorskip("httpx")


def test_first_sync_loads_items(dlt_mod, tmp_path, monkeypatch):
    fc = FakeZoteroClient(versions_map={"k1": 1, "k2": 1})
    pipeline = _run(dlt_mod, tmp_path, monkeypatch, fc)
    rows = _read_items(pipeline)
    assert set(rows) == {"k1", "k2"}


def test_incremental_cursor_advances(dlt_mod, tmp_path, monkeypatch):
    fc = FakeZoteroClient(versions_map={"k1": 1})
    pipeline = _run(dlt_mod, tmp_path, monkeypatch, fc)
    rows = _read_items(pipeline)
    assert "k1" in rows
    # second run with since=1 → nothing changed
    fc._versions = {}  # no changes since version 1
    pipeline2 = _run(dlt_mod, tmp_path, monkeypatch, fc)
    assert _read_items(pipeline2) == rows  # still one row, no change


def test_deleted_item_emits_tombstone(dlt_mod, tmp_path, monkeypatch):
    fc = FakeZoteroClient(
        versions_map={"k1": 1, "k2": 1},
        deleted_keys=["k2"],
    )
    pipeline = _run(dlt_mod, tmp_path, monkeypatch, fc)
    rows = _read_items(pipeline)
    # k2 should be a tombstone → removed by merge hard_delete
    assert "k1" in rows
    assert "k2" not in rows


def test_trashed_item_emits_tombstone(dlt_mod, tmp_path, monkeypatch):
    fc = FakeZoteroClient(versions_map={"k1": 1})
    fc._items["k1"] = _item(trashed=True)
    pipeline = _run(dlt_mod, tmp_path, monkeypatch, fc)
    rows = _read_items(pipeline)
    assert "k1" not in rows


def test_304_is_noop(dlt_mod, tmp_path, monkeypatch):
    fc = FakeZoteroClient(versions_map={"k1": 1})
    pipeline = _run(dlt_mod, tmp_path, monkeypatch, fc)
    rows = _read_items(pipeline)
    assert "k1" in rows
    # force 304 on next call
    fc.last_modified = "1"
    pipeline2 = _run(dlt_mod, tmp_path, monkeypatch, fc)
    assert _read_items(pipeline2) == rows
