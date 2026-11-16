"""The runnable example must reject returned pipeline errors and finish pending loads."""

import importlib.util
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

_SPEC = importlib.util.spec_from_file_location(
    "onenote_example", Path(__file__).parents[1] / "examples" / "onenote_example.py"
)
example = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(example)


@pytest.mark.parametrize("status", ["PipelineRunCompleted", "PipelineRunAlreadyCompleted"])
def test_completion(status):
    example.require_completed(SimpleNamespace(status=status, data_ingestion_info=[]), "add")


@pytest.mark.parametrize(
    "result",
    [
        {},
        SimpleNamespace(status="PipelineRunErrored"),
        SimpleNamespace(status="PipelineRunStarted"),
        SimpleNamespace(status="PipelineRunCompleted", data_ingestion_info=[{"error": "failed"}]),
    ],
)
def test_returned_errors(result):
    with pytest.raises(RuntimeError):
        example.require_completed(result, "add")


@pytest.mark.asyncio
async def test_pending_load_requires_current_extraction(monkeypatch):
    sources = []

    def source(*args, **kwargs):
        item = SimpleNamespace(cognee_sync_stats={})
        sources.append(item)
        return item

    completed = SimpleNamespace(status="PipelineRunCompleted", data_ingestion_info=[])

    async def add(item, **kwargs):
        if len(sources) == 2:
            item.cognee_sync_stats.update(failed=0, complete=1)
        assert kwargs["write_disposition"] == "merge"
        return completed

    monkeypatch.setattr(example, "onenote_source", source)
    monkeypatch.setattr(example.cognee, "add", AsyncMock(side_effect=add))
    monkeypatch.setattr(example.cognee, "cognify", AsyncMock(return_value=completed))
    await example.sync("token", "account", ["book"], "dataset")
    assert len(sources) == 2
    example.cognee.cognify.assert_awaited_once()


@pytest.mark.asyncio
async def test_ingestion_failure_stops_processing(monkeypatch):
    monkeypatch.setattr(example, "onenote_source", lambda *args, **kwargs: object())
    monkeypatch.setattr(
        example.cognee, "add", AsyncMock(return_value=SimpleNamespace(status="PipelineRunErrored"))
    )
    processing = AsyncMock()
    monkeypatch.setattr(example.cognee, "cognify", processing)
    with pytest.raises(RuntimeError):
        await example.sync("token", "account", ["book"], "dataset")
    processing.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("stats", [{"failed": 1, "complete": 0}, {"failed": 0, "complete": 0}])
async def test_incomplete_sync_stops_processing(monkeypatch, stats):
    monkeypatch.setattr(
        example, "onenote_source", lambda *args, **kwargs: SimpleNamespace(cognee_sync_stats=stats)
    )
    completed = SimpleNamespace(status="PipelineRunCompleted", data_ingestion_info=[])
    monkeypatch.setattr(example.cognee, "add", AsyncMock(return_value=completed))
    processing = AsyncMock()
    monkeypatch.setattr(example.cognee, "cognify", processing)
    with pytest.raises(RuntimeError, match="interrupted"):
        await example.sync("token", "account", ["book"], "dataset")
    processing.assert_not_awaited()


@pytest.mark.asyncio
async def test_each_notebook_is_ingested_before_processing(monkeypatch):
    selected = []

    def source(token, *, notebook_id, account_id):
        selected.append(notebook_id)
        return SimpleNamespace(cognee_sync_stats={"failed": 0, "complete": 1})

    completed = SimpleNamespace(status="PipelineRunCompleted", data_ingestion_info=[])
    monkeypatch.setattr(example, "onenote_source", source)
    monkeypatch.setattr(example.cognee, "add", AsyncMock(return_value=completed))
    monkeypatch.setattr(example.cognee, "cognify", AsyncMock(return_value=completed))
    await example.sync("token", "account", ["first", "second"], "dataset")
    assert selected == ["first", "second"]
    assert example.cognee.add.await_count == 2
    example.cognee.cognify.assert_awaited_once()


@pytest.mark.asyncio
async def test_repeated_pending_load_stops_processing(monkeypatch):
    monkeypatch.setattr(
        example, "onenote_source", lambda *args, **kwargs: SimpleNamespace(cognee_sync_stats={})
    )
    completed = SimpleNamespace(status="PipelineRunCompleted", data_ingestion_info=[])
    monkeypatch.setattr(example.cognee, "add", AsyncMock(return_value=completed))
    processing = AsyncMock()
    monkeypatch.setattr(example.cognee, "cognify", processing)
    with pytest.raises(RuntimeError, match="No current"):
        await example.sync("token", "account", ["book"], "dataset")
    assert example.cognee.add.await_count == 2
    processing.assert_not_awaited()
