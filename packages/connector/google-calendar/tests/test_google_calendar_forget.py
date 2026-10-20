"""End-to-end: Google Calendar changes must reach cognee's graph, not just dlt rows.

Runs the real cognee pipeline (dlt merge -> document ingestion -> cognify)
against the fake Calendar API, with a mocked LLM and mocked embeddings, so no
network or API key is needed.  Each event carries one distinctive company
name, which the mocked LLM turns into exactly one graph entity.
"""

import importlib
from datetime import datetime

import cognee
import pytest
import pytest_asyncio
from cognee.infrastructure.databases.graph import get_graph_engine
from cognee.infrastructure.databases.vector.embeddings.LiteLLMEmbeddingEngine import (
    LiteLLMEmbeddingEngine,
)
from cognee.infrastructure.llm import LLMGateway
from fake_calendar import FakeCalendar

from cognee_community_connector_google_calendar import google_calendar_source

add_data_points_module = importlib.import_module("cognee.tasks.storage.add_data_points")

DATASET = "google_calendar_forget_test"
ALPHA = "Alphacorp"
BRAVO = "Bravocorp"
CHARLIE = "Charliecorp"
TOKENS = (ALPHA, BRAVO, CHARLIE)
START = datetime(2026, 10, 12, 10, 0)


def _with_required(model, **values):
    """Build a cognee data model, filling any other required text field.

    cognee swaps in Node and KnowledgeGraph models with extra required fields
    (label, summary, description) when the configured LLM provider is Gemini,
    so the mock must work with both variants.
    """
    for field_name, field in model.model_fields.items():
        if field.is_required() and field_name not in values:
            values[field_name] = "mock"
    return model(**values)


async def _mock_structured_output(
    text_input=None, system_prompt=None, response_model=str, **_kwargs
):
    """Extract one entity named after whichever token appears in the chunk text."""
    from cognee.shared.data_models import KnowledgeGraph, SummarizedContent
    from cognee.shared.data_models import Node as KGNode

    if response_model is str:
        return "Mocked answer."
    if response_model == SummarizedContent:
        return SummarizedContent(summary="Mock summary", description="Mock summary")
    if response_model == KnowledgeGraph:
        name = next((t for t in TOKENS if text_input and t in text_input), None)
        nodes = (
            [
                _with_required(
                    KGNode, id=name, name=name, type="Company", description=f"{name} entity"
                )
            ]
            if name
            else []
        )
        return _with_required(KnowledgeGraph, nodes=nodes, edges=[])
    return response_model()


async def _graph_has(token: str) -> bool:
    nodes, _ = await (await get_graph_engine()).get_graph_data()
    token = token.lower()
    return any(
        token in str(value).lower() for _nid, props in nodes for value in (props or {}).values()
    )


@pytest_asyncio.fixture
async def clean_environment(tmp_path, monkeypatch):
    monkeypatch.setenv("COGNEE_SKIP_CONNECTION_TEST", "true")
    cognee.config.data_root_directory(str(tmp_path / "data"))
    cognee.config.system_root_directory(str(tmp_path / "system"))
    cognee.config.set_relational_db_config({"db_provider": "sqlite"})

    async def _noop_index(*_a, **_k):
        return None

    monkeypatch.setattr(add_data_points_module, "index_data_points", _noop_index)
    monkeypatch.setattr(add_data_points_module, "index_graph_edges", _noop_index)
    monkeypatch.setattr(LLMGateway, "acreate_structured_output", _mock_structured_output)

    async def _mock_embed_text(self, text):
        return [[0.0] * self.get_vector_size() for _ in text]

    monkeypatch.setattr(LiteLLMEmbeddingEngine, "embed_text", _mock_embed_text)

    await cognee.prune.prune_data()
    await cognee.prune.prune_system(metadata=True)
    yield
    await cognee.prune.prune_data()
    await cognee.prune.prune_system(metadata=True)


async def _sync(calendar):
    await cognee.add(
        google_calendar_source(service=calendar),
        dataset_name=DATASET,
        primary_key="id",
        write_disposition="merge",
        max_rows_per_table=0,
    )
    await cognee.cognify(datasets=[DATASET])


@pytest.mark.asyncio
async def test_deleted_event_is_forgotten_and_a_cancelled_occurrence_keeps_the_series(
    clean_environment,
):
    calendar = FakeCalendar()
    series = calendar.put_series(
        "primary",
        "Daily sync",
        START,
        "RRULE:FREQ=DAILY;COUNT=3",
        description=f"{ALPHA} builds the delivery drone.",
    )
    calendar.put_event("primary", "Finance review", START, description=f"{BRAVO} pays for it.")
    gone = calendar.put_event("primary", "Rocket demo", START, description=f"{CHARLIE} rockets.")

    await _sync(calendar)
    for token in TOKENS:
        assert await _graph_has(token), f"{token} should be in the graph after the first sync"

    calendar.cancel_occurrence("primary", series, START.replace(day=13))
    calendar.delete_event("primary", gone)
    await _sync(calendar)

    assert await _graph_has(ALPHA), "cancelling one occurrence must keep the series"
    assert await _graph_has(BRAVO), "an untouched event must stay"
    assert not await _graph_has(CHARLIE), "a deleted event must be forgotten from the graph"
