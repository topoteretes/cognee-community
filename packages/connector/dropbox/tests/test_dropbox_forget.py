"""End-to-end: Dropbox changes must reach cognee's graph, not just dlt rows.

Runs the real cognee pipeline (dlt merge -> document ingestion -> cognify) with
the in-memory Dropbox, a mocked LLM and mocked embeddings, so no network or
API key is needed.  Each file carries one distinctive company name, which the
mocked LLM turns into exactly one graph entity.
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
from fake_dropbox import FakeDropbox

from cognee_community_connector_dropbox import dropbox_source

add_data_points_module = importlib.import_module("cognee.tasks.storage.add_data_points")

DATASET = "dropbox_forget_test"
ALPHA = "Alphacorp"
BRAVO = "Bravocorp"
CHARLIE = "Charliecorp"
TOKENS = (ALPHA, BRAVO, CHARLIE)


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


async def _sync(fake):
    await cognee.add(
        dropbox_source(client=fake),
        dataset_name=DATASET,
        primary_key="id",
        write_disposition="merge",
        max_rows_per_table=0,
    )
    await cognee.cognify(datasets=[DATASET])


@pytest.mark.asyncio
async def test_deleted_file_and_folder_are_forgotten_moved_file_is_kept(clean_environment):
    fake = FakeDropbox()
    fake.put("/Notes/alpha.md", f"{ALPHA} is a company in the logistics sector.")
    fake.put("/Notes/bravo.txt", f"{BRAVO} is an unrelated company in finance.")
    fake.put("/Old/Deep/charlie.md", f"{CHARLIE} builds rockets.")

    await _sync(fake)
    for token in TOKENS:
        assert await _graph_has(token), f"{token} should be in the graph after the first sync"

    # Move alpha, delete bravo, delete the whole /Old folder.
    fake.move("/Notes/alpha.md", "/Archive/alpha.md")
    fake.delete("/Notes/bravo.txt")
    fake.delete("/Old")
    await _sync(fake)

    assert await _graph_has(ALPHA), "a moved file must stay in memory"
    assert not await _graph_has(BRAVO), "a deleted file must be forgotten from the graph"
    assert not await _graph_has(CHARLIE), "files in a deleted folder must be forgotten"
