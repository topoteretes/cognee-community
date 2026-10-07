"""End to end through cognee: a meeting deleted in Zoom leaves the graph on the next sync.

Runs the documented ``cognee.remember`` call twice against the fake Zoom API,
with the LLM and embeddings mocked, and checks the graph after each sync.
"""

import importlib

import cognee
import pytest
import pytest_asyncio
from cognee.infrastructure.databases.graph import get_graph_engine
from cognee.infrastructure.databases.vector.embeddings.LiteLLMEmbeddingEngine import (
    LiteLLMEmbeddingEngine,
)
from cognee.infrastructure.llm import LLMGateway

from cognee_community_connector_zoom import zoom_source

add_data_points_module = importlib.import_module("cognee.tasks.storage.add_data_points")

DATASET = "zoom_forget_test"
ALPHA = "Alphacorp"
BRAVO = "Bravocorp"


def _vtt(text):
    return f"WEBVTT\n\n1\n00:00:01.000 --> 00:00:04.000\nPriya Shah: {text}\n"


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
        name = next((t for t in (ALPHA, BRAVO) if text_input and t in text_input), None)
        nodes = (
            [KGNode(id=name, name=name, type="Company", description=f"{name} entity")]
            if name
            else []
        )
        return KnowledgeGraph(nodes=nodes, edges=[])
    return response_model()


async def _graph_has(token: str) -> bool:
    nodes, _ = await (await get_graph_engine()).get_graph_data()
    token = token.lower()
    return any(
        token in str(value).lower() for _nid, props in nodes for value in (props or {}).values()
    )


@pytest_asyncio.fixture
async def clean_environment(tmp_path, monkeypatch, fixed_now):
    pytest.importorskip("dlt")
    pytest.importorskip("ladybug")

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


async def _sync(zoom):
    await cognee.remember(
        zoom_source(client=zoom),
        dataset_name=DATASET,
        primary_key="id",
        write_disposition="merge",
        max_rows_per_table=0,
        self_improvement=False,
    )


@pytest.mark.asyncio
async def test_deleting_a_recording_forgets_its_graph_content(clean_environment, zoom):
    zoom.add_meeting("m1", transcript=_vtt(f"{ALPHA} signed the logistics contract."))
    zoom.add_meeting("m2", topic="Retro", transcript=_vtt(f"{BRAVO} is moving to finance."))

    await _sync(zoom)
    assert await _graph_has(ALPHA), "m1 entity should be in the graph after the first sync"
    assert await _graph_has(BRAVO), "m2 entity should be in the graph after the first sync"

    del zoom.meetings["m2"]
    await _sync(zoom)

    assert await _graph_has(ALPHA), "the surviving meeting must stay in the graph"
    assert not await _graph_has(BRAVO), "the deleted meeting must leave the graph"
