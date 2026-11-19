"""Exercise the Bitbucket DLT source through Cognee's real ingestion pipeline.

The Bitbucket HTTP boundary, LLM, and embedding calls are local fakes. Cognee's
data persistence, document routing, cognify, vector and graph indexing, search,
and orphan cleanup remain real and use isolated temporary directories.
"""

import cognee
import pytest
import pytest_asyncio
from cognee.infrastructure.databases.graph import get_graph_engine
from cognee.infrastructure.databases.vector.embeddings.LiteLLMEmbeddingEngine import (
    LiteLLMEmbeddingEngine,
)
from cognee.infrastructure.llm import LLMGateway
from test_bitbucket import FakeBitbucketSession, _pr

from cognee_community_connector_bitbucket import bitbucket_source

DATASET = "bitbucket_pipeline_integration_test"
ALPHA = "BitbucketAlphacorp"
BRAVO = "BitbucketBravocorp"
GLOBEX = "BitbucketGlobex"
_LLM_INPUTS: list[str] = []


async def _mock_structured_output(
    text_input=None, _system_prompt=None, response_model=str, **_kwargs
):
    _LLM_INPUTS.append(str(text_input or ""))
    from cognee.shared.data_models import KnowledgeGraph, SummarizedContent
    from cognee.shared.data_models import Node as KGNode

    if response_model is str:
        return "Mocked graph answer."
    if response_model == SummarizedContent:
        return SummarizedContent(summary="Mock summary", description="Mock summary")
    if response_model == KnowledgeGraph:
        name = next(
            (token for token in (ALPHA, BRAVO, GLOBEX) if text_input and token in text_input),
            None,
        )
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
        token in str(value).lower()
        for _node_id, properties in nodes
        for value in (properties or {}).values()
    )


@pytest_asyncio.fixture
async def isolated_cognee(tmp_path, monkeypatch):
    pytest.importorskip("dlt")
    pytest.importorskip("ladybug")

    monkeypatch.setenv("COGNEE_SKIP_CONNECTION_TEST", "true")
    isolated_home = tmp_path / "home"
    isolated_home.mkdir()
    monkeypatch.setenv("HOME", str(isolated_home))
    monkeypatch.setenv("CACHE_ROOT_DIRECTORY", str(tmp_path / "cache"))
    from cognee.base_config import get_base_config

    get_base_config.cache_clear()
    cognee.config.data_root_directory(str(tmp_path / "data"))
    cognee.config.system_root_directory(str(tmp_path / "system"))
    cognee.config.set_relational_db_config({"db_provider": "sqlite"})
    get_base_config().cache_root_directory = str(tmp_path / "cache")

    monkeypatch.setattr(LLMGateway, "acreate_structured_output", _mock_structured_output)

    async def _mock_embed_text(self, text):
        return [[0.0] * self.get_vector_size() for _ in text]

    monkeypatch.setattr(LiteLLMEmbeddingEngine, "embed_text", _mock_embed_text)

    await cognee.prune.prune_data()
    await cognee.prune.prune_system(metadata=True)
    yield
    await cognee.prune.prune_data()
    await cognee.prune.prune_system(metadata=True)


async def _sync(session):
    await cognee.add(
        bitbucket_source(
            workspace="acme",
            repositories=["repo"],
            content_types=["pull_requests"],
            session=session,
        ),
        dataset_name=DATASET,
        primary_key="id",
        write_disposition="merge",
        max_rows_per_table=0,
    )
    await cognee.cognify(datasets=[DATASET])


@pytest.mark.asyncio
async def test_real_cognee_ingest_cognify_search_and_delete(isolated_cognee):
    alpha_pr = _pr(1)
    alpha_pr["description"] = {"raw": f"{ALPHA} builds logistics software."}
    bravo_pr = _pr(2)
    bravo_pr["description"] = {"raw": f"{BRAVO} builds finance software."}
    session = FakeBitbucketSession(prs=[alpha_pr, bravo_pr])

    await _sync(session)
    assert await _graph_has(ALPHA)
    assert await _graph_has(BRAVO)

    search_start = len(_LLM_INPUTS)
    search_result = await cognee.search(
        query_text=f"What does {ALPHA} build?",
        query_type=cognee.SearchType.GRAPH_COMPLETION,
        datasets=[DATASET],
        session_id="initial-graph-search",
    )
    assert search_result
    search_inputs = _LLM_INPUTS[search_start:]
    assert any(ALPHA in text for text in search_inputs), "Cognee search should query graph evidence"

    # Exercise the real vector retrieval path too. Embeddings are deterministic
    # zero vectors in this isolated test, so this checks stored payload content
    # and cleanup rather than semantic ranking quality.
    search_start = len(_LLM_INPUTS)
    vector_result = await cognee.search(
        query_text=f"What does {BRAVO} build?",
        query_type=cognee.SearchType.RAG_COMPLETION,
        datasets=[DATASET],
        session_id="initial-vector-search",
    )
    assert vector_result
    vector_inputs = _LLM_INPUTS[search_start:]
    assert any("finance software" in text for text in vector_inputs), (
        "vector search should retrieve indexed PR content"
    )

    changed_pr = _pr(1, updated_on="2026-09-10T00:00:00+00:00")
    changed_pr["description"] = {"raw": f"{GLOBEX} builds logistics software."}
    session.prs = [changed_pr]
    await _sync(session)
    assert await _graph_has(GLOBEX), "updated pull request content must reach the graph"
    assert not await _graph_has(ALPHA), "superseded pull request content must leave the graph"
    assert not await _graph_has(BRAVO), "deleted pull request entity must be removed from graph"

    search_start = len(_LLM_INPUTS)
    deleted_result = await cognee.search(
        query_text=f"What does {BRAVO} build?",
        query_type=cognee.SearchType.RAG_COMPLETION,
        datasets=[DATASET],
        session_id="deleted-vector-search",
    )
    assert deleted_result
    deleted_inputs = _LLM_INPUTS[search_start:]
    assert all("finance software" not in text for text in deleted_inputs), (
        "deleted PR content must leave vector search context"
    )

    # Query through Cognee again after update and orphan cleanup.
    search_start = len(_LLM_INPUTS)
    final_result = await cognee.search(
        query_text=f"What does {GLOBEX} build?",
        query_type=cognee.SearchType.GRAPH_COMPLETION,
        datasets=[DATASET],
        session_id="updated-graph-search",
    )
    assert final_result
    final_inputs = _LLM_INPUTS[search_start:]
    assert any(GLOBEX in text for text in final_inputs), "updated content should be searchable"
    assert all(BRAVO not in text for text in final_inputs), "deleted content must not be searchable"
