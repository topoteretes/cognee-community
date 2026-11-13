"""Real DLT persistence and Cognee document conversion, with fake Helix HTTP."""

import importlib.util
import json
import sqlite3
from pathlib import Path
from unittest.mock import AsyncMock
from uuid import uuid4

import dlt
import httpx
import pytest
from cognee.tasks.ingestion import dlt_utils
from cognee.tasks.ingestion.dlt_row_data import DltRowData
from cognee.tasks.ingestion.resolve_dlt_sources import _build_document_data_item
from cognee_community_connector_twitch import twitch_source
from dlt.destinations import sqlalchemy
from test_twitch import NOW, Helix


def source(fake, **kwargs):
    return twitch_source(
        "secret",
        "app",
        http_client=httpx.Client(transport=httpx.MockTransport(fake.handler)),
        **kwargs,
    )


def test_resource_contract_and_cognee_document_conversion():
    resource = source(Helix())
    assert getattr(resource, dlt_utils.DOCUMENT_SOURCE_ATTR) == "twitch"
    assert getattr(resource, dlt_utils.PIPELINE_SCOPE_ATTR) == "twitch_documents"
    schema = resource.compute_table_schema()
    assert schema["write_disposition"] == "merge"
    assert schema["columns"]["_deleted"]["hard_delete"] is True
    assert schema["columns"]["cognee_node_set"]["data_type"] == "json"
    rows = list(resource)
    assert len(rows) == 3
    for row in rows:
        record = DltRowData(
            table_name="twitch_documents",
            primary_key_columns=["id"],
            primary_key_value=row["id"],
            row_data=row,
            content_hash="hash",
            schema_info={},
            schema_hash="schema",
            foreign_keys=[],
            dlt_db_name="local",
            dataset_name="twitch",
        )
        item = _build_document_data_item(record, uuid4(), "twitch")
        assert item.node_set == ["twitch:channel:coder"]
        assert item.system_metadata["external_id"] == row["id"]
        assert item.system_metadata["source"] == "twitch"
        assert row["title"] in item.data
        assert item.literal_text


def test_real_dlt_merge_persistent_state_and_final_hard_delete(tmp_path):
    fake = Helix()
    database = tmp_path / "staging.sqlite"
    pipeline = dlt.pipeline(
        pipeline_name="twitch_test",
        pipelines_dir=str(tmp_path / "pipelines"),
        destination=sqlalchemy(credentials=f"sqlite:///{database}"),
        dataset_name="twitch",
    )

    def run():
        with httpx.Client(transport=httpx.MockTransport(fake.handler)) as client:
            resource = twitch_source("secret", "app", http_client=client)
            result = pipeline.run(resource)
            assert not result.has_failed_jobs
            assert not client.is_closed

    def stored():
        with sqlite3.connect(tmp_path / "staging__twitch.sqlite") as connection:
            return {
                row[0]: json.loads(row[1])
                for row in connection.execute("SELECT id, content FROM twitch_documents")
            }

    run()
    assert set(stored()) == {"channel:10", "video:v1", "clip:c1"}
    initial_state = json.dumps(pipeline.state, default=str)
    assert "secret" not in initial_state
    assert "cursor" in initial_state and "v1" in initial_state
    run()
    assert len(stored()) == 3
    fake.videos.clear()
    run()
    assert set(stored()) == {"channel:10", "clip:c1"}
    fake.follows.clear()
    run()
    assert stored() == {}


@pytest.mark.parametrize(
    "kwargs",
    [
        {"resource_name": "Bad Name"},
        {"resource_name": "a__b"},
        {"lookback_days": 0},
        {"lookback_days": True},
        {"lookback_days": 1.5},
    ],
)
def test_invalid_configuration(kwargs):
    with pytest.raises(ValueError):
        source(Helix(), **kwargs)


def test_missing_credentials_and_old_core(monkeypatch):
    with pytest.raises(ValueError, match="access token"):
        twitch_source("", "app")
    monkeypatch.setattr(dlt_utils, "DOCUMENT_SYNC_VERSION", 1)
    with pytest.raises(RuntimeError, match="document sync v2"):
        source(Helix())


def test_host_revocation_preserves_state_and_caller_client():
    fake, state = Helix(), {}
    api = fake.api()
    from cognee_community_connector_twitch.twitch import _sync

    rows = dlt_utils.guarded_rows(_sync(api, state, NOW, 30), lambda: None)
    next(rows)
    rows.close()
    assert state == {}
    api.client.close()
    with httpx.Client(transport=httpx.MockTransport(fake.handler)) as client:
        assert len(list(twitch_source("secret", "app", http_client=client))) == 3
        assert not client.is_closed


@pytest.mark.asyncio
async def test_example_remember_and_recall_wiring(monkeypatch):
    path = Path(__file__).parents[1] / "examples" / "example.py"
    spec = importlib.util.spec_from_file_location("twitch_example", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setenv("TWITCH_CLIENT_ID", "app")
    monkeypatch.setenv("TWITCH_ACCESS_TOKEN", "secret")
    remember, recall = AsyncMock(), AsyncMock(return_value="Rust")
    monkeypatch.setattr(module.cognee, "remember", remember)
    monkeypatch.setattr(module.cognee, "recall", recall)
    monkeypatch.setattr(module.sys, "argv", ["example.py", "Rust?"])
    await module.main()
    remember.assert_awaited_once()
    assert remember.call_args.kwargs == {
        "dataset_name": "twitch",
        "primary_key": "id",
        "write_disposition": "merge",
        "max_rows_per_table": 0,
    }
    assert getattr(remember.call_args.args[0], dlt_utils.DOCUMENT_SOURCE_ATTR) == "twitch"
    recall.assert_awaited_once_with("Rust?", datasets=["twitch"])
