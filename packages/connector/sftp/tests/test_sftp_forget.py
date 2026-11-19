"""End to end through cognee: a file deleted on the server leaves the graph on the next sync.

Runs the documented ``cognee.remember`` call twice against the fake SFTP server,
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
from conftest import HOST

from cognee_community_connector_sftp import sftp_source

try:
    # cognee 1.6.5 and later read the token count off the result.
    from cognee.infrastructure.databases.vector.embeddings.EmbeddingEngine import (
        EmbeddingResult,
    )
except ImportError:
    EmbeddingResult = None

add_data_points_module = importlib.import_module("cognee.tasks.storage.add_data_points")

DATASET = "sftp_forget_test"
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
        token in str(value).lower() for _nid, props in nodes for value in (props or {}).values()
    )


@pytest_asyncio.fixture
async def clean_environment(tmp_path, monkeypatch):
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
        vectors = [[0.0] * self.get_vector_size() for _ in text]
        return EmbeddingResult(vectors, tokens=0) if EmbeddingResult else vectors

    monkeypatch.setattr(LiteLLMEmbeddingEngine, "embed_text", _mock_embed_text)

    await cognee.prune.prune_data()
    await cognee.prune.prune_system(metadata=True)
    yield
    await cognee.prune.prune_data()
    await cognee.prune.prune_system(metadata=True)


async def _sync(sftp):
    await cognee.remember(
        sftp_source(host=HOST, roots=["/reports"], client=sftp),
        dataset_name=DATASET,
        primary_key="id",
        write_disposition="merge",
        max_rows_per_table=0,
        self_improvement=False,
    )


@pytest.mark.asyncio
async def test_deleting_a_file_forgets_its_graph_content(clean_environment, sftp):
    sftp.write("/reports/logistics.md", f"{ALPHA} signed the logistics contract.")
    sftp.write("/reports/2026/finance.txt", f"{BRAVO} is moving to finance.")

    await _sync(sftp)
    assert await _graph_has(ALPHA), "the first file's entity should be in the graph"
    assert await _graph_has(BRAVO), "the second file's entity should be in the graph"

    sftp.remove("/reports/2026/finance.txt")
    await _sync(sftp)

    assert await _graph_has(ALPHA), "the surviving file must stay in the graph"
    assert not await _graph_has(BRAVO), "the deleted file must leave the graph"
