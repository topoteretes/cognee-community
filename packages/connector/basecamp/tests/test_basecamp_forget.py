"""Forget-on-delete must remove an item's cognified graph content, not just its
Data record. Ingests two Basecamp documents through cognee (LLM and embeddings
mocked, no live credentials), then:

* trashes one: the next incremental sync must forget its graph entity;
* purges another from the trash before any sync saw it: only the
  reconciliation sweep can catch that, so the next full sweep must forget it.

Same approach as the Google Drive connector's forget test.
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

from cognee_community_connector_basecamp import basecamp_source

add_data_points_module = importlib.import_module("cognee.tasks.storage.add_data_points")

DATASET = "basecamp_forget_test"
UA = "cognee-basecamp-tests (test@example.com)"
# Distinctive tokens so each document maps to exactly one graph entity.
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
    for _nid, props in nodes:
        if any(token in str(v).lower() for v in (props or {}).values()):
            return True
    return False


@pytest_asyncio.fixture
async def clean_environment(tmp_path, monkeypatch):
    dlt = pytest.importorskip("dlt")
    pytest.importorskip("ladybug")

    # Keep dlt's pipeline state (cursor, known ids) inside this test's tmp dir, so
    # it neither leaks between tests nor touches ~/.dlt (same as cognee's own
    # dlt ingestion tests).
    pipeline_factory = dlt.pipeline
    monkeypatch.setattr(
        dlt,
        "pipeline",
        lambda **kw: pipeline_factory(pipelines_dir=str(tmp_path / "pipelines"), **kw),
    )

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


async def _sync(fake, **kwargs):
    await cognee.add(
        basecamp_source(
            "999",
            access_token="token",
            user_agent=UA,
            http_client=fake.http_client(),
            full_sync_every=0,
            **kwargs,
        ),
        dataset_name=DATASET,
        write_disposition="merge",
        max_rows_per_table=0,
    )
    await cognee.cognify(datasets=[DATASET])


@pytest.mark.asyncio
async def test_trashed_and_purged_items_are_forgotten_from_the_graph(clean_environment, fake):
    # One test on purpose (like the Google Drive forget test): cognee keeps its
    # graph engine for the whole process, so the scenarios share one environment.
    fake.add("Document", "Alpha notes", f"<p>{ALPHA} is a company in logistics.</p>")
    bravo = fake.add("Document", "Bravo notes", f"<p>{BRAVO} is a company in finance.</p>")
    charlie = fake.add("Document", "Charlie notes", f"<p>{CHARLIE} is a company in retail.</p>")

    await _sync(fake)
    for token in TOKENS:
        assert await _graph_has(token), f"{token} entity should be in the graph after ingest"

    # 1. Trashed in Basecamp: the next incremental sync forgets it.
    fake.set_status(bravo["id"], "trashed")
    await _sync(fake)
    assert not await _graph_has(BRAVO), "trashed Bravo entity must be removed from the graph"
    assert await _graph_has(ALPHA), "surviving Alpha entity must remain"

    # 2. Trashed and emptied from the trash before any sync saw it. An incremental
    #    run cannot see that; the reconciliation sweep must forget it.
    fake.set_status(charlie["id"], "trashed")
    fake.purge(charlie["id"])
    await _sync(fake)
    assert await _graph_has(CHARLIE), "an incremental run cannot see a purge"

    await _sync(fake, full_sync=True)
    assert not await _graph_has(CHARLIE), "purged Charlie entity must be removed by the sweep"
    assert await _graph_has(ALPHA), "surviving Alpha entity must remain"
