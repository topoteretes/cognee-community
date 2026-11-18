"""End-to-end: ingest Raindrop bookmarks into cognee, then forget a deleted one.

Runs the real dlt + cognee pipeline against a fake Raindrop API, with the LLM
and embeddings mocked (no live credentials). Covers the acceptance criteria
that only show up past the connector boundary: bookmarks become searchable
graph content, an incremental re-sync leaves untouched bookmarks alone, and a
bookmark trashed upstream disappears from the graph on the next sync.
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
from fake_raindrop import FakeRaindrop, bookmark

from cognee_community_connector_raindrop import raindrop_source

add_data_points_module = importlib.import_module("cognee.tasks.storage.add_data_points")

DATASET = "raindrop_forget_test"
# Distinctive tokens so each bookmark maps to exactly one graph entity.
ALPHA = "Alphacorp"
BRAVO = "Bravocorp"


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
        token in str(value).lower() for _node_id, props in nodes for value in (props or {}).values()
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


async def _sync(api):
    await cognee.add(
        raindrop_source(client=api.client()),
        dataset_name=DATASET,
        write_disposition="merge",
    )
    await cognee.cognify(datasets=[DATASET])


async def _dataset_titles():
    from cognee.modules.data.methods import get_dataset_data, get_datasets_by_name
    from cognee.modules.users.methods import get_default_user

    user = await get_default_user()
    datasets = await get_datasets_by_name([DATASET], user.id)
    return sorted(data.name for data in await get_dataset_data(datasets[0].id))


@pytest.mark.asyncio
async def test_trashing_a_bookmark_forgets_its_graph_content(clean_environment):
    api = FakeRaindrop()
    api.put(bookmark(1, "Logistics notes", note=f"{ALPHA} is a company in logistics."))
    api.put(bookmark(2, "Finance notes", note=f"{BRAVO} is a company in finance."))

    await _sync(api)
    assert await _graph_has(ALPHA), "bookmark 1 entity should be in the graph after ingest"
    assert await _graph_has(BRAVO), "bookmark 2 entity should be in the graph after ingest"
    assert len(await _dataset_titles()) == 2

    # Trash bookmark 2 upstream; the next incremental sync must forget it.
    api.trash_bookmark(2)
    await _sync(api)

    assert await _graph_has(ALPHA), "untouched bookmark must survive the re-sync"
    assert not await _graph_has(BRAVO), (
        "trashed bookmark's entity must be removed from the graph (forget-on-delete)"
    )
    assert len(await _dataset_titles()) == 1
