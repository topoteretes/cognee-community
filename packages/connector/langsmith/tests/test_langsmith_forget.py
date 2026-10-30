"""End-to-end verification through cognee: deleted LangSmith prompts leave the memory graph."""

import importlib
import cognee
import pytest
import pytest_asyncio
from cognee.infrastructure.databases.graph import get_graph_engine
from cognee.infrastructure.databases.vector.embeddings.LiteLLMEmbeddingEngine import (
    LiteLLMEmbeddingEngine,
)
from cognee.infrastructure.llm import LLMGateway
from cognee_community_connector_langsmith import langsmith_source

try:
    from cognee.infrastructure.databases.vector.embeddings.EmbeddingEngine import (
        EmbeddingResult,
    )
except ImportError:
    EmbeddingResult = None

add_data_points_module = importlib.import_module("cognee.tasks.storage.add_data_points")

DATASET = "langsmith_forget_test"
ALPHA_PROMPT = "AlphaPromptExtractor"
BETA_PROMPT = "BetaPromptClassifier"


async def _mock_structured_output(text_input=None, system_prompt=None, response_model=str, **_kwargs):
    from cognee.shared.data_models import KnowledgeGraph, SummarizedContent
    from cognee.shared.data_models import Node as KGNode

    if response_model is str:
        return "Mocked LangSmith insight."
    if response_model == SummarizedContent:
        return SummarizedContent(summary="LangSmith prompt summary", description="LangSmith prompt description")
    if response_model == KnowledgeGraph:
        name = next((t for t in (ALPHA_PROMPT, BETA_PROMPT) if text_input and t in text_input), None)
        nodes = (
            [KGNode(id=name, name=name, type="Prompt", description=f"{name} template")]
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


async def _sync(langsmith):
    await cognee.remember(
        langsmith_source(client=langsmith, resources=["prompts"]),
        dataset_name=DATASET,
        primary_key="id",
        write_disposition="merge",
        max_rows_per_table=0,
        self_improvement=False,
    )


@pytest.mark.asyncio
async def test_deleting_prompt_forgets_its_graph_content(clean_environment, langsmith):
    langsmith.add_prompt("alpha_handle", "Extract JSON from text", f"{ALPHA_PROMPT} handles ingestion extraction.")
    beta = langsmith.add_prompt("beta_handle", "Classify incoming intent", f"{BETA_PROMPT} classifies queries.")

    await _sync(langsmith)
    assert await _graph_has(ALPHA_PROMPT), "Alpha prompt entity should be present in memory graph"
    assert await _graph_has(BETA_PROMPT), "Beta prompt entity should be present in memory graph"

    del langsmith.prompts[beta["handle"]]
    await _sync(langsmith)

    assert await _graph_has(ALPHA_PROMPT), "Alpha prompt should remain in memory graph"
    assert not await _graph_has(BETA_PROMPT), "Deleted Beta prompt entity must be forgotten from memory graph"
