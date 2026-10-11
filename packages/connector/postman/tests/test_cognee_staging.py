"""Real Cognee staging/read-back, without LLMs or an authenticated Postman account."""

import asyncio
import importlib
from types import SimpleNamespace

import pytest
from test_postman import FakeClient, source


@pytest.fixture
def ingestion(tmp_path, monkeypatch):
    import dlt

    module = importlib.import_module("cognee.tasks.ingestion.ingest_dlt_source")
    monkeypatch.setattr(
        module,
        "get_relational_config",
        lambda: SimpleNamespace(
            db_provider="sqlite",
            db_path=str(tmp_path),
            db_host="",
            db_port=0,
            db_username="",
            db_password="",
        ),
    )
    monkeypatch.setattr(
        module,
        "get_dlt_destination",
        lambda dlt_db_name: dlt.destinations.sqlalchemy(
            credentials={
                "database": str(tmp_path / dlt_db_name),
                "drivername": "sqlite",
            }
        ),
    )
    factory = dlt.pipeline
    monkeypatch.setattr(
        dlt,
        "pipeline",
        lambda **kwargs: factory(pipelines_dir=str(tmp_path / "pipelines"), **kwargs),
    )
    return module


def test_cognee_reads_retained_and_empty_snapshots(ingestion):
    client = FakeClient()

    async def run(**kwargs):
        return await ingestion.ingest_dlt_source(
            source(client, **kwargs),
            "dataset",
            primary_key="id",
            write_disposition="replace",
            max_rows_per_table=0,
        )

    rows = asyncio.run(run())
    assert len(rows) == 2
    original = [(row.primary_key_value, row.row_data["content"]) for row in rows]
    # Simulate a later ingestion/cleanup failure by not acknowledging these rows
    # anywhere in Cognee; the next no-change sync must still return the corpus.
    client.calls.clear()
    replay = asyncio.run(run())
    assert [(row.primary_key_value, row.row_data["content"]) for row in replay] == original
    assert not any(path.startswith("/collections/") for path, _ in client.calls)
    client.tree["item"] = []
    client.inventory["1-c"]["updatedAt"] = "final-request-deleted"
    empty = asyncio.run(run())
    assert not empty
    assert set(empty.loaded_tables) == {source(client).name}
    replay_empty = asyncio.run(run())
    assert not replay_empty and set(replay_empty.loaded_tables) == set(empty.loaded_tables)


def test_cognee_scopes_other_owner_snapshot(ingestion):
    from cognee_community_connector_postman import postman_source

    client = FakeClient()

    async def run(owner):
        src = postman_source(
            client=client,
            workspace_id="w",
            account_id="1",
            corpus_owner=owner,
            dataset_name="dataset",
        )
        return await ingestion.ingest_dlt_source(
            src,
            "dataset",
            write_disposition="replace",
            max_rows_per_table=0,
        )

    alice = asyncio.run(run("alice"))
    bob = asyncio.run(run("bob"))
    assert set(alice.loaded_tables).isdisjoint(bob.loaded_tables)
    assert {row.primary_key_value for row in alice}.isdisjoint(
        {row.primary_key_value for row in bob}
    )
