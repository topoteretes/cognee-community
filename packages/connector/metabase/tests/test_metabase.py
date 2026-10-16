"""Metabase rendering, incremental state, and forget-on-delete without network."""

import asyncio
import importlib
import json
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import NAMESPACE_OID, uuid5

import httpx
import pytest
from cognee.tasks.ingestion.dlt_utils import document_source_tag

from cognee_community_connector_metabase import MetabaseUnchanged, metabase_source
from cognee_community_connector_metabase.metabase import _API, _item_to_row, _sync

BASE_URL = "https://example.test/metabase"


def source(api, **kwargs):
    return metabase_source(
        BASE_URL, client=api.client, request_interval=0, **({"api_key": "test-key"} | kwargs)
    )


def read_rows(pipeline):
    with (
        pipeline.sql_client() as client,
        client.execute_query("SELECT id, title, content, url FROM metabase_documents") as cursor,
    ):
        return {
            row[0]: dict(zip(("id", "title", "content", "url"), row, strict=True))
            for row in cursor.fetchall()
        }


def test_initial_full_ingest(metabase_api, pipeline_factory):
    pipeline = pipeline_factory()
    src = source(metabase_api)
    assert document_source_tag(src) == "metabase"
    pipeline.run(src)
    rows = read_rows(pipeline)
    assert set(rows) == {"metabase:source", "collection:1", "card:1", "dashboard:1"}
    assert "Team metrics" in rows["collection:1"]["content"]
    assert "SELECT sum(total) FROM orders" in rows["card:1"]["content"]
    assert "Revenue (card: 1)" in rows["dashboard:1"]["content"]
    assert rows["card:1"]["url"] == BASE_URL + "/question/1"


@pytest.mark.parametrize("kind", ["collection", "card", "dashboard"])
def test_incremental_sync_only_renders_changed(kind, metabase_api, pipeline_factory, monkeypatch):
    pipeline = pipeline_factory()
    pipeline.run(source(metabase_api))
    before = read_rows(pipeline)
    metabase_api.data[kind][0].update(updated_at="2026-02-01T00:00:00Z", description="Edited")
    metabase_api.requests.clear()
    module = importlib.import_module("cognee_community_connector_metabase.metabase")
    rendered = []
    original = module._item_to_row

    def record(kind, item, base_url):
        rendered.append(kind)
        return original(kind, item, base_url)

    monkeypatch.setattr(module, "_item_to_row", record)
    # Recreate the pipeline to prove the cursor survives source/pipeline objects.
    pipeline = pipeline_factory()
    pipeline.run(source(metabase_api))
    after = read_rows(pipeline)
    assert rendered == [kind]
    assert "Edited" in after[f"{kind}:1"]["content"]
    assert all(after[key] == row for key, row in before.items() if key != f"{kind}:1")
    details = [r.url.path for r in metabase_api.requests if r.url.path.endswith("/1")]
    assert details == ([] if kind == "collection" else [f"/metabase/api/{kind}/1"])
    state = pipeline.state["sources"]["metabase"]["metabase_snapshot"]
    assert state["cursor"] == "2026-02-01T00:00:00+00:00"


def test_unchanged_is_noop(metabase_api, pipeline_factory):
    pipeline = pipeline_factory()
    pipeline.run(source(metabase_api))
    before = read_rows(pipeline)
    metabase_api.requests.clear()
    with pytest.raises(Exception, match="unchanged") as caught:
        pipeline.run(source(metabase_api))
    cause = caught.value
    while cause is not None and not isinstance(cause, MetabaseUnchanged):
        cause = cause.__cause__
    assert isinstance(cause, MetabaseUnchanged)
    assert read_rows(pipeline) == before
    assert len(metabase_api.requests) == 3
    # A no-op extraction must not prevent a later real update.
    metabase_api.data["card"][0].update(updated_at="2026-03-01T00:00:00Z", name="New name")
    pipeline.run(source(metabase_api))
    assert read_rows(pipeline)["card:1"]["title"] == "New name"


@pytest.mark.parametrize("delete_all", [False, True])
def test_forget_on_delete_purges_graph(delete_all, metabase_api, pipeline_factory, monkeypatch):
    pipeline = pipeline_factory()
    pipeline.run(source(metabase_api))
    before = read_rows(pipeline)
    if delete_all:
        for kind in metabase_api.data:
            metabase_api.data[kind] = []
    else:
        metabase_api.data["card"] = []
    pipeline.run(source(metabase_api))
    after = read_rows(pipeline)
    assert "card:1" not in after
    assert "metabase:source" in after
    if delete_all:
        assert set(after) == {"metabase:source"}

    # Exercise the real document resolver and orphan cleanup with staging rows;
    # mock only the persistence/graph boundaries (no LLM or graph server).
    resolver = importlib.import_module("cognee.tasks.ingestion.resolve_dlt_sources")

    def row_data(row):
        return SimpleNamespace(
            table_name="metabase_documents",
            primary_key_value=row["id"],
            row_data=row,
            content_hash=json.dumps(row, sort_keys=True),
        )

    def identifier(row):
        return uuid5(NAMESPACE_OID, resolver._dlt_row_identifier(row_data(row)))

    monkeypatch.setattr(
        resolver,
        "ingest_dlt_source",
        AsyncMock(return_value=[row_data(row) for row in after.values()]),
    )
    monkeypatch.setattr(
        resolver,
        "get_unique_data_id",
        AsyncMock(side_effect=lambda key, user: uuid5(NAMESPACE_OID, key)),
    )
    methods = importlib.import_module("cognee.modules.data.methods")
    monkeypatch.setattr(
        methods,
        "get_authorized_existing_datasets",
        AsyncMock(return_value=[SimpleNamespace(id="dataset")]),
    )
    get_data = importlib.import_module("cognee.modules.data.methods.get_dataset_data")
    monkeypatch.setattr(
        get_data,
        "get_dataset_data",
        AsyncMock(
            return_value=[
                SimpleNamespace(id=identifier(row), external_metadata={"source": "metabase"})
                for row in before.values()
            ]
        ),
    )
    graph = importlib.import_module("cognee.modules.graph.methods.delete_data_nodes_and_edges")
    delete = importlib.import_module("cognee.modules.data.methods.delete_data")
    purge_graph, purge_data = AsyncMock(), AsyncMock()
    monkeypatch.setattr(graph, "delete_data_nodes_and_edges", purge_graph)
    monkeypatch.setattr(delete, "delete_data", purge_data)

    async def resolve_and_clean():
        items, cleanup = await resolver.resolve_dlt_sources(
            source(metabase_api), "metabase", SimpleNamespace(id="user")
        )
        assert all(item.external_metadata["source"] == "metabase" for item in items)
        assert cleanup is not None
        await cleanup()

    asyncio.run(resolve_and_clean())
    removed = set(before) - set(after)
    assert purge_graph.await_count == len(removed)
    assert purge_data.await_count == len(removed)
    for key in removed:
        purge_graph.assert_any_await("dataset", identifier(before[key]), "user")


def test_session_auth_and_logout(metabase_api, pipeline_factory):
    pipeline_factory().run(source(metabase_api, api_key=None, username="admin", password="secret"))
    login = metabase_api.requests[0]
    assert login.method == "POST"
    assert json.loads(login.content) == {"username": "admin", "password": "secret"}
    assert metabase_api.requests[-1].method == "DELETE"
    assert all("x-api-key" not in r.headers for r in metabase_api.requests)
    assert not metabase_api.client.is_closed


def test_api_key_preferred(metabase_api, pipeline_factory):
    pipeline_factory().run(source(metabase_api, username="admin", password="secret"))
    assert all("/session" not in r.url.path for r in metabase_api.requests)


def test_failed_listing_preserves_snapshot_and_cursor(metabase_api, pipeline_factory):
    pipeline = pipeline_factory()
    pipeline.run(source(metabase_api))
    before = read_rows(pipeline)
    state = deepcopy(pipeline.state["sources"])
    metabase_api.data["card"] = []
    metabase_api.failures["dashboard"] = [403]
    with pytest.raises(Exception, match="403"):
        pipeline.run(source(metabase_api))
    assert read_rows(pipeline) == before
    assert pipeline.state["sources"] == state


def test_retry_429_and_pacing(metabase_api, monkeypatch):
    sleeps = []
    monkeypatch.setattr("cognee_community_connector_metabase.metabase.time.sleep", sleeps.append)
    metabase_api.failures["collection"] = [429]
    api = _API(metabase_api.client, BASE_URL, 0.1)
    api.headers["x-api-key"] = "test-key"
    assert len(list(api.listing("collection"))) == 1
    assert sleeps == [0.1, 0, 0.1]


def test_new_item_below_cursor_and_missing_timestamp(metabase_api):
    api = _API(metabase_api.client, BASE_URL, 0)
    api.headers["x-api-key"] = "test-key"
    state = {}
    _sync(api, state, ("collection",))
    metabase_api.data["collection"].append(
        {"id": 2, "name": "Older", "updated_at": "2020-01-01T00:00:00Z"}
    )
    assert len(_sync(api, state, ("collection",))) == 3
    del metabase_api.data["collection"][0]["updated_at"]
    metabase_api.data["collection"][0]["description"] = "No timestamp edit"
    rows = _sync(api, state, ("collection",))
    assert "No timestamp edit" in next(r for r in rows if r["id"] == "collection:1")["content"]


def test_paginated_envelope():
    def handle(request):
        offset = int(request.url.params["offset"])
        return httpx.Response(200, json={"data": [{"id": offset}], "total": 2, "offset": offset})

    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        assert list(_API(client, BASE_URL, 0).listing("card")) == [{"id": 0}, {"id": 1}]


def test_incomplete_listing_aborts():
    with (
        httpx.Client(
            transport=httpx.MockTransport(
                lambda r: httpx.Response(200, json={"data": [], "total": 10})
            )
        ) as client,
        pytest.raises(ValueError, match="Incomplete"),
    ):
        list(_API(client, BASE_URL, 0).listing("card"))


def test_query_builder_and_dashboard_text():
    row = _item_to_row(
        "card",
        {"id": 3, "dataset_query": {"type": "query", "query": {"source-table": 5}}},
        BASE_URL,
    )
    assert '"source-table": 5' in row["content"]
    row = _item_to_row(
        "dashboard",
        {"id": 4, "ordered_cards": [{"visualization_settings": {"text": "Board notes"}}]},
        BASE_URL,
    )
    assert "Board notes" in row["content"]


def test_environment_auth(monkeypatch, metabase_api):
    monkeypatch.setenv("METABASE_URL", BASE_URL)
    monkeypatch.setenv("METABASE_API_KEY", "test-key")
    assert document_source_tag(metabase_source(client=metabase_api.client)) == "metabase"


@pytest.mark.parametrize(
    "kwargs",
    [
        {"base_url": "invalid", "api_key": "key"},
        {"base_url": BASE_URL},
        {"base_url": BASE_URL, "api_key": "key", "kinds": []},
    ],
)
def test_invalid_configuration(kwargs):
    with pytest.raises(ValueError):
        metabase_source(**kwargs)
