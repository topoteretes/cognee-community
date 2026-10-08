"""Keep tests that run cognee inside disposable storage roots, with the LLM mocked."""

import cognee
import pytest_asyncio

# tokens the mocked llm turns into one graph entity each
ENTITY_TOKENS = ("Alphaperson", "Bravoperson")


def _reset_cached_engines() -> None:
    from cognee.context_global_variables import graph_db_config, vector_db_config
    from cognee.infrastructure.databases.graph.get_graph_engine import _create_graph_engine
    from cognee.infrastructure.databases.relational.create_relational_engine import (
        create_relational_engine,
    )
    from cognee.infrastructure.databases.vector.create_vector_engine import _create_vector_engine
    from cognee.tasks.ingestion.get_dlt_destination import get_dlt_destination
    from dlt.common.configuration.container import Container
    from dlt.common.pipeline import PipelineContext

    Container()[PipelineContext].deactivate()
    _create_graph_engine.cache_clear()
    _create_vector_engine.cache_clear()
    create_relational_engine.cache_clear()
    get_dlt_destination.cache_clear()
    graph_db_config.set(None)
    vector_db_config.set(None)


@pytest_asyncio.fixture
async def clean_environment(tmp_path, monkeypatch):
    monkeypatch.setenv("COGNEE_SKIP_CONNECTION_TEST", "true")
    monkeypatch.setenv("DB_PATH", str(tmp_path / "databases"))
    monkeypatch.setenv("DLT_DATA_DIR", str(tmp_path / "dlt"))
    monkeypatch.setenv("PIPELINES_DIR", str(tmp_path / "dlt" / "pipelines"))
    _reset_cached_engines()
    cognee.config.data_root_directory(str(tmp_path / "data"))
    cognee.config.system_root_directory(str(tmp_path / "system"))
    cognee.config.set_relational_db_config({"db_provider": "sqlite"})
    await cognee.prune.prune_data()
    await cognee.prune.prune_system(metadata=True)

    yield

    await cognee.prune.prune_data()
    await cognee.prune.prune_system(metadata=True)
    _reset_cached_engines()


async def _mock_structured_output(
    text_input=None, system_prompt=None, response_model=str, **_kwargs
):
    from cognee.shared.data_models import KnowledgeGraph, SummarizedContent
    from cognee.shared.data_models import Node as KGNode

    if response_model is str:
        return "Mocked answer."
    if response_model == SummarizedContent:
        return SummarizedContent(summary="Mock summary", description="Mock summary")
    if response_model == KnowledgeGraph:
        name = next((t for t in ENTITY_TOKENS if text_input and t in text_input), None)
        nodes = [KGNode(id=name, name=name, type="Person", description=name)] if name else []
        return KnowledgeGraph(nodes=nodes, edges=[])
    return response_model()


async def _mock_embed_text(self, text):
    return [[0.0] * self.get_vector_size() for _ in text]


@pytest_asyncio.fixture
async def mocked_llm(clean_environment, monkeypatch):
    from cognee.infrastructure.databases.vector.embeddings.LiteLLMEmbeddingEngine import (
        LiteLLMEmbeddingEngine,
    )
    from cognee.infrastructure.llm import LLMGateway

    # vector indexing still runs (on zero vectors) so recall has chunks to search
    monkeypatch.setattr(LLMGateway, "acreate_structured_output", _mock_structured_output)
    monkeypatch.setattr(LiteLLMEmbeddingEngine, "embed_text", _mock_embed_text)
