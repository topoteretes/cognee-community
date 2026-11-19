import importlib.util
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import cognee
import pytest

import cognee_community_connector_airtable as connector


def example_module():
    path = Path(__file__).resolve().parents[2] / "examples" / "example.py"
    spec = importlib.util.spec_from_file_location("airtable_example", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.asyncio
async def test_example_executes_remember_and_dataset_scoped_recall(monkeypatch, capsys):
    monkeypatch.setenv("AIRTABLE_BASE_ID", "appOne")
    monkeypatch.setenv("AIRTABLE_ACCESS_TOKEN", "example-private-token")
    monkeypatch.setenv("AIRTABLE_DATASET", "airtable_acceptance")
    monkeypatch.setenv("AIRTABLE_TABLE_IDS", "tblOne, tblTwo")
    monkeypatch.setenv("AIRTABLE_INCLUDE_COMMENTS", "false")
    monkeypatch.setenv("AIRTABLE_QUESTION", "Which cargo is stored?")
    source = object()
    factory = Mock(return_value=source)
    remember = AsyncMock(return_value=SimpleNamespace(status="completed"))
    recall = AsyncMock(return_value=[SimpleNamespace(text="Retrieved passage.")])
    monkeypatch.setattr(connector, "airtable_source", factory)
    monkeypatch.setattr(cognee, "remember", remember)
    monkeypatch.setattr(cognee, "recall", recall)
    await example_module().main()
    assert factory.call_args.kwargs["base_id"] == "appOne"
    assert factory.call_args.kwargs["table_ids"] == ["tblOne", "tblTwo"]
    assert factory.call_args.kwargs["include_comments"] is False
    remember.assert_awaited_once_with(
        source,
        dataset_name="airtable_acceptance",
        self_improvement=False,
        dlt_config={"primary_key": "id", "write_disposition": "merge", "max_rows_per_table": 0},
    )
    recall.assert_awaited_once_with(
        "Which cargo is stored?",
        datasets=["airtable_acceptance"],
        query_type=cognee.SearchType.CHUNKS,
    )
    assert "Retrieved passage." in capsys.readouterr().out


@pytest.mark.asyncio
async def test_example_validates_environment_before_ingestion(monkeypatch):
    monkeypatch.delenv("AIRTABLE_BASE_ID", raising=False)
    monkeypatch.delenv("AIRTABLE_ACCESS_TOKEN", raising=False)
    remember = AsyncMock()
    monkeypatch.setattr(cognee, "remember", remember)
    with pytest.raises(SystemExit, match="AIRTABLE_BASE_ID"):
        await example_module().main()
    remember.assert_not_awaited()


@pytest.mark.asyncio
async def test_example_does_not_retrieve_after_unsuccessful_memory_build(monkeypatch):
    monkeypatch.setenv("AIRTABLE_BASE_ID", "appOne")
    monkeypatch.setenv("AIRTABLE_ACCESS_TOKEN", "example-private-token")
    monkeypatch.setattr(connector, "airtable_source", Mock(return_value=object()))
    monkeypatch.setattr(
        cognee, "remember", AsyncMock(return_value=SimpleNamespace(status="failed"))
    )
    recall = AsyncMock()
    monkeypatch.setattr(cognee, "recall", recall)
    with pytest.raises(RuntimeError, match="did not complete"):
        await example_module().main()
    recall.assert_not_awaited()
