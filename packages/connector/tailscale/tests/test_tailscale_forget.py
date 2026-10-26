"""End-to-end verification through cognee: deleted Tailscale devices leave the memory graph."""

import importlib
import cognee
import pytest
import pytest_asyncio
from cognee.infrastructure.databases.graph import get_graph_engine
from cognee.infrastructure.databases.vector.embeddings.LiteLLMEmbeddingEngine import (
    LiteLLMEmbeddingEngine,
)
from cognee.infrastructure.llm import LLMGateway
from cognee_community_connector_tailscale import tailscale_source

try:
    from cognee.infrastructure.databases.vector.embeddings.EmbeddingEngine import (
        EmbeddingResult,
    )
except ImportError:
    EmbeddingResult = None

add_data_points_module = importlib.import_module("cognee.tasks.storage.add_data_points")

DATASET = "tailscale_forget_test"
ALPHA_NODE = "AlphaGatewayNode"
BETA_NODE = "BetaAnalyticsNode"


async def _mock_structured_output(text_input=None, system_prompt=None, response_model=str, **_kwargs):
    from cognee.shared.data_models import KnowledgeGraph, SummarizedContent
    from cognee.shared.data_models import Node as KGNode

    if response_model is str:
        return "Mocked Tailscale insight."
    if response_model == SummarizedContent:
        return SummarizedContent(summary="Tailscale device summary", description="Tailscale device description")
    if response_model == KnowledgeGraph:
        name = next((t for t in (ALPHA_NODE, BETA_NODE) if text_input and t in text_input), None)
        nodes = (
            [KGNode(id=name, name=name, type="NetworkNode", description=f"{name} tailnet machine")]
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


async def _sync(tailscale):
    await cognee.remember(
        tailscale_source(client=tailscale, tailnet="-", resources=["devices"]),
        dataset_name=DATASET,
        primary_key="id",
        write_disposition="merge",
        max_rows_per_table=0,
        self_improvement=False,
    )


@pytest.mark.asyncio
async def test_deleting_device_forgets_its_graph_content(clean_environment, tailscale):
    tailscale.add_device("node_alpha", "Alpha Gateway Machine", f"{ALPHA_NODE} routes primary production VPC traffic.")
    beta = tailscale.add_device("node_beta", "Beta Analytics Machine", f"{BETA_NODE} serves internal analytics workloads.")

    await _sync(tailscale)
    assert await _graph_has(ALPHA_NODE), "Alpha node entity should be present in memory graph"
    assert await _graph_has(BETA_NODE), "Beta node entity should be present in memory graph"

    del tailscale.devices[beta["id"]]
    await _sync(tailscale)

    assert await _graph_has(ALPHA_NODE), "Alpha node should remain in memory graph"
    assert not await _graph_has(BETA_NODE), "Deleted Beta node entity must be forgotten from memory graph"
