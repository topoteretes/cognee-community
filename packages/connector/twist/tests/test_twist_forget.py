"""Forget-on-delete must hard-delete a thread: its Data record and its graph
content, rather than leaving an emptied document behind. Ingests Twist threads
through cognee (LLM and embeddings mocked, no live credentials), then:

* deletes a thread in Twist: the next sync must drop its Data record and its
  graph entity, and the other threads keep their Data ids;
* deletes the only comment that mentions an entity: re-rendering the thread
  must forget that entity, while the thread itself stays.

Same approach as the Google Drive and Basecamp connectors' forget tests.
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
from cognee.modules.data.methods import get_authorized_existing_datasets
from cognee.modules.data.methods.get_dataset_data import get_dataset_data
from cognee.modules.users.methods import get_default_user

from cognee_community_connector_twist import twist_source

add_data_points_module = importlib.import_module("cognee.tasks.storage.add_data_points")

DATASET = "twist_forget_test"
# Distinctive tokens so each piece of text maps to exactly one graph entity.
ALPHA = "Alphacorp"
BRAVO = "Bravocorp"
CHARLIE = "Charliecorp"
TOKENS = (ALPHA, BRAVO, CHARLIE)


async def _mock_structured_output(
    text_input=None, system_prompt=None, response_model=str, **_kwargs
):
    """Extract one entity per token that appears in the chunk text."""
    from cognee.shared.data_models import KnowledgeGraph, SummarizedContent
    from cognee.shared.data_models import Node as KGNode

    if response_model is str:
        return "Mocked answer."
    if response_model == SummarizedContent:
        return SummarizedContent(summary="Mock summary", description="Mock summary")
    if response_model == KnowledgeGraph:
        names = [t for t in TOKENS if text_input and t in text_input]
        nodes = [
            KGNode(id=name, name=name, type="Company", description=f"{name} entity")
            for name in names
        ]
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

    # Keep dlt's pipeline state (known threads) inside this test's tmp dir, so it
    # neither leaks between tests nor touches ~/.dlt.
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
        vectors = [[0.0] * self.get_vector_size() for _ in text]
        try:
            # Newer cognee reads the token count off the result.
            from cognee.infrastructure.databases.vector.embeddings.EmbeddingEngine import (
                EmbeddingResult,
            )
        except ImportError:
            return vectors
        return EmbeddingResult(vectors, tokens=0)

    monkeypatch.setattr(LiteLLMEmbeddingEngine, "embed_text", _mock_embed_text)

    await cognee.prune.prune_data()
    await cognee.prune.prune_system(metadata=True)
    yield
    await cognee.prune.prune_data()
    await cognee.prune.prune_system(metadata=True)


async def _documents() -> dict[str, object]:
    """The dataset's Data records, by the row id the connector gave them."""
    user = await get_default_user()
    (dataset,) = await get_authorized_existing_datasets(
        user=user, permission_type="read", datasets=[DATASET]
    )
    return {
        data.system_metadata["external_id"]: data.id
        for data in await get_dataset_data(dataset.id)
        if isinstance(data.system_metadata, dict)
    }


async def _sync(fake):
    await cognee.add(
        twist_source(
            fake.workspace_id,
            access_token=fake.token,
            http_client=fake.http_client(),
            full_sync_every=0,
        ),
        dataset_name=DATASET,
        write_disposition="merge",
    )
    await cognee.cognify(datasets=[DATASET])


@pytest.mark.asyncio
async def test_deleted_thread_and_comment_are_forgotten_from_the_graph(clean_environment, fake):
    # One test on purpose (like the Google Drive forget test): cognee keeps its
    # graph engine for the whole process, so the scenarios share one environment.
    fake.add_channel(852087, "launch")
    fake.add_thread(852087, "Partners", f"{ALPHA} is our logistics partner.")
    bravo = fake.add_thread(852087, "Vendors", f"{BRAVO} supplies the payment terminals.")
    review = fake.add_thread(852087, "Review", "Who should audit checkout?")
    audit = fake.add_comment(review["id"], f"{CHARLIE} will run the security audit.")

    await _sync(fake)
    for token in TOKENS:
        assert await _graph_has(token), f"{token} entity should be in the graph after ingest"
    before = await _documents()
    assert f"thread:{bravo['id']}" in before

    # 1. Thread deleted in Twist: the next sync forgets it.
    fake.delete_thread(bravo["id"])
    await _sync(fake)
    survivors = {k: v for k, v in before.items() if k != f"thread:{bravo['id']}"}
    assert await _documents() == survivors, (
        "the deleted thread's Data record must go and the others must keep their ids"
    )
    assert not await _graph_has(BRAVO), "deleted thread's entity must be removed from the graph"
    assert await _graph_has(ALPHA), "surviving thread's entity must remain"

    # 2. The only comment naming Charliecorp is deleted: the thread is re-rendered
    #    without it, so the entity goes while the thread stays.
    fake.delete_comment(review["id"], audit["id"])
    await _sync(fake)
    assert not await _graph_has(CHARLIE), "deleted comment's entity must be removed"
    assert await _graph_has(ALPHA), "surviving thread's entity must remain"
