"""Forget-on-delete end to end: a task deleted in Wrike leaves cognee's graph.

Two tasks go through the real ``cognee.remember`` path (dlt merge, document
mode, cognify, dlt state kept between runs). The LLM and the embeddings are
mocked, so no key is needed and each task yields exactly one entity named
after a unique token. Deleting one task in the fake account must remove that
entity on the next sync, while the other stays.
"""

import importlib
from datetime import UTC, datetime

import cognee
import pytest
import pytest_asyncio
from cognee.infrastructure.databases.graph import get_graph_engine
from cognee.infrastructure.databases.vector.embeddings.LiteLLMEmbeddingEngine import (
    LiteLLMEmbeddingEngine,
)
from cognee.infrastructure.llm import LLMGateway
from dlt.common.configuration.container import Container
from dlt.common.pipeline import PipelineContext
from fake_wrike import FakeAccount

from cognee_community_connector_wrike import wrike_source

add_data_points_module = importlib.import_module("cognee.tasks.storage.add_data_points")

ALPHA = "Alphacorp"
BRAVO = "Bravocorp"
llm_calls = []


async def _mock_structured_output(text_input=None, system_prompt=None, response_model=str, **_):
    """Extract one entity named after whichever token appears in the chunk text."""
    from cognee.shared.data_models import KnowledgeGraph, SummarizedContent
    from cognee.shared.data_models import Node as KGNode

    llm_calls.append(response_model)
    if response_model is str:
        return "Mocked answer."
    if response_model == SummarizedContent:
        return SummarizedContent(summary="Mock summary", description="Mock summary")
    if response_model == KnowledgeGraph:
        name = next((t for t in (ALPHA, BRAVO) if text_input and t in text_input), None)
        nodes = [KGNode(id=name, name=name, type="Company", description=name)] if name else []
        return KnowledgeGraph(nodes=nodes, edges=[])
    return response_model()


async def _graph_has(token: str) -> bool:
    nodes, _ = await (await get_graph_engine()).get_graph_data()
    return any(
        token.lower() in str(value).lower()
        for _node_id, props in nodes
        for value in (props or {}).values()
    )


@pytest_asyncio.fixture
async def clean_environment(tmp_path, monkeypatch):
    # Without an LLM key cognee would switch to its local keyless extractor and
    # never call the mocked LLM; this setting keeps the configured (mocked) path.
    monkeypatch.setenv("COGNEE_SKIP_CONNECTION_TEST", "true")
    # dlt keeps pipeline state (our cursor and seen ids) in its data dir, by
    # default ~/.dlt; give every test a fresh one.
    monkeypatch.setenv("DLT_DATA_DIR", str(tmp_path / "dlt"))
    cognee.config.data_root_directory(str(tmp_path / "data"))
    cognee.config.system_root_directory(str(tmp_path / "system"))

    async def _noop(*_a, **_k):
        return None

    async def _mock_embed_text(self, text):
        return [[0.0] * self.get_vector_size() for _ in text]

    monkeypatch.setattr(add_data_points_module, "index_data_points", _noop)
    monkeypatch.setattr(add_data_points_module, "index_graph_edges", _noop)
    monkeypatch.setattr(LLMGateway, "acreate_structured_output", _mock_structured_output)
    monkeypatch.setattr(LiteLLMEmbeddingEngine, "embed_text", _mock_embed_text)
    llm_calls.clear()

    await cognee.prune.prune_data()
    await cognee.prune.prune_system(metadata=True)
    yield
    await cognee.prune.prune_data()
    await cognee.prune.prune_system(metadata=True)
    # remember() leaves its dlt pipeline active in the process; later tests that
    # iterate a resource directly would otherwise read this test's state.
    Container()[PipelineContext].deactivate()


async def _sync(account, dataset):
    # Each test uses its own dataset, so its own dlt pipeline and state: dlt
    # keeps a pipeline's state in memory for the life of the process.
    await cognee.remember(
        wrike_source(client=account),
        dataset_name=dataset,
        primary_key="id",
        write_disposition="merge",
        max_rows_per_table=0,
        self_improvement=False,
    )
    account.tick()


@pytest.mark.asyncio
async def test_deleting_a_task_in_wrike_forgets_its_graph_content(clean_environment):
    account = FakeAccount(start=datetime.now(UTC))
    account.tasks.clear()
    account.add_task("TA", ["A"], description=f"{ALPHA} is moving its warehouse.")
    account.add_task("TB", ["B"], description=f"{BRAVO} is auditing its accounts.")
    account.tick()

    await _sync(account, "wrike_forget_two_tasks")
    assert any(model.__name__ == "KnowledgeGraph" for model in llm_calls), (
        "the mocked LLM must do the extraction (not the keyless local extractor)"
    )
    assert await _graph_has(ALPHA), "task TA's entity should be in the graph after ingest"
    assert await _graph_has(BRAVO), "task TB's entity should be in the graph after ingest"

    account.delete_task("TB")
    await _sync(account, "wrike_forget_two_tasks")

    assert await _graph_has(ALPHA), "the surviving task's entity must stay"
    assert not await _graph_has(BRAVO), "the deleted task's entity must leave the graph"


@pytest.mark.asyncio
async def test_deleting_the_only_task_still_forgets_it(clean_environment):
    # A table emptied by delete rows must still forget its last document, so
    # the account holds one task and no folders (folders are rows too).
    account = FakeAccount(start=datetime.now(UTC))
    account.tasks.clear()
    account.folders["ROOT"]["childIds"].clear()
    account.add_task("TB", ["ROOT"], description=f"{BRAVO} is auditing its accounts.")
    account.tick()

    await _sync(account, "wrike_forget_only_task")
    assert await _graph_has(BRAVO)

    account.delete_task("TB")
    await _sync(account, "wrike_forget_only_task")
    assert not await _graph_has(BRAVO)
