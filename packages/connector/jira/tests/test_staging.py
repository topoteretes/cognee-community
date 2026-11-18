"""Cognee's actual dlt read-back contract, using temporary SQLite storage."""

import asyncio
import importlib
from types import SimpleNamespace

import dlt
import pytest

from .test_jira import Reader, source


@pytest.fixture
def ingestion(tmp_path, monkeypatch):
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
            credentials={"database": str(tmp_path / dlt_db_name), "drivername": "sqlite"}
        ),
    )
    factory = dlt.pipeline
    monkeypatch.setattr(
        dlt, "pipeline", lambda **kw: factory(pipelines_dir=str(tmp_path / "pipelines"), **kw)
    )
    return module


def test_read_back_unchanged_failure_and_declared_empty(ingestion):
    client = Reader()

    def run(**kwargs):
        return asyncio.run(
            ingestion.ingest_dlt_source(
                source(client, **kwargs),
                "dataset",
                primary_key="id",
                write_disposition="replace",
                max_rows_per_table=0,
            )
        )

    original = run()
    assert len(original) == 5 and len(original.loaded_tables) == 1
    assert all(item.row_data["content"] for item in original)
    replay = run()
    assert [x.row_data for x in replay] == [x.row_data for x in original]
    client.fail = lambda p, q: p.endswith("/comment")
    with pytest.raises(Exception, match="Synthetic failure"):
        run()
    client.fail = None
    assert [x.row_data for x in run()] == [x.row_data for x in original]
    client.issues = {}
    empty = run(missing_policy="mirror_visibility")
    assert not empty and set(empty.loaded_tables) == set(original.loaded_tables)
    assert not run()


def test_two_datasets_have_independent_staging(ingestion):
    client = Reader()

    def run(dataset, **kwargs):
        return asyncio.run(
            ingestion.ingest_dlt_source(
                source(client, **kwargs),
                dataset,
                primary_key="id",
                write_disposition="replace",
                max_rows_per_table=0,
            )
        )

    one = run("one")
    two = run("two")
    assert len(one) == len(two) == 5
    client.issues = {}
    assert not run("one", missing_policy="mirror_visibility")
    with pytest.raises(Exception, match="disappeared"):
        run("two")


def test_cold_empty_has_authoritative_cleanup_scope(ingestion):
    client = Reader()
    client.issues = {}
    src = source(client, missing_policy="mirror_visibility")
    result = asyncio.run(
        ingestion.ingest_dlt_source(
            src, "cold_empty", primary_key="id", write_disposition="replace", max_rows_per_table=0
        )
    )
    assert not result and result.loaded_tables == frozenset({src.name})
