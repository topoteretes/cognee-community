"""End-to-end verification through cognee: deleted Rippling employees leave the memory graph."""

import importlib
import cognee
import pytest
import pytest_asyncio
from cognee.infrastructure.databases.graph import get_graph_engine
from cognee.infrastructure.databases.vector.embeddings.LiteLLMEmbeddingEngine import (
    LiteLLMEmbeddingEngine,
)
from cognee.infrastructure.llm import LLMGateway
from cognee_community_connector_rippling import rippling_source

try:
    from cognee.infrastructure.databases.vector.embeddings.EmbeddingEngine import (
        EmbeddingResult,
    )
except ImportError:
    EmbeddingResult = None

add_data_points_module = importlib.import_module("cognee.tasks.storage.add_data_points")

DATASET = "rippling_forget_test"
ALPHA_EMP = "AlphaDeveloper"
BETA_EMP = "BetaDesigner"


async def _mock_structured_output(text_input=None, system_prompt=None, response_model=str, **_kwargs):
    from cognee.shared.data_models import KnowledgeGraph, SummarizedContent
    from cognee.shared.data_models import Node as KGNode

    if response_model is str:
        return "Mocked Rippling insight."
    if response_model == SummarizedContent:
        return SummarizedContent(summary="Rippling employee summary", description="Rippling employee description")
    if response_model == KnowledgeGraph:
        name = next((t for t in (ALPHA_EMP, BETA_EMP) if text_input and t in text_input), None)
        nodes = (
            [KGNode(id=name, name=name, type="Employee", description=f"{name} team member")]
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


async def _sync(rippling):
    await cognee.remember(
        rippling_source(client=rippling, resources=["employees"]),
        dataset_name=DATASET,
        primary_key="id",
        write_disposition="merge",
        max_rows_per_table=0,
        self_improvement=False,
    )


@pytest.mark.asyncio
async def test_deleting_employee_forgets_its_graph_content(clean_environment, rippling):
    rippling.add_employee("emp_alpha", "Alpha Developer", f"{ALPHA_EMP} builds backend data engines.")
    beta = rippling.add_employee("emp_beta", "Beta Designer", f"{BETA_EMP} creates UI design systems.")

    await _sync(rippling)
    assert await _graph_has(ALPHA_EMP), "Alpha employee entity should be present in memory graph"
    assert await _graph_has(BETA_EMP), "Beta employee entity should be present in memory graph"

    del rippling.employees[beta["id"]]
    await _sync(rippling)

    assert await _graph_has(ALPHA_EMP), "Alpha employee should remain in memory graph"
    assert not await _graph_has(BETA_EMP), "Deleted Beta employee entity must be forgotten from memory graph"
