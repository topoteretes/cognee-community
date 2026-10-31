"""Forget-on-delete must remove a file's cognified graph content, not just its
Data record.  Ingests two Egnyte files, cognifies them (LLM and embeddings
mocked, no credentials), deletes one file's whole folder in Egnyte, and checks
that its entity is gone from the graph while the other file's entity remains.

Deleting the folder (rather than the file) is deliberate: Egnyte reports it as a
single folder event, so this also covers finding the files under it.
"""

import importlib

import cognee
import pytest
import pytest_asyncio
from _fake_egnyte import FakeEgnyte
from cognee.infrastructure.databases.graph import get_graph_engine
from cognee.infrastructure.databases.vector.embeddings.get_embedding_engine import (
    create_embedding_engine,
)
from cognee.infrastructure.llm import LLMGateway

from cognee_community_connector_egnyte import egnyte_source

add_data_points_module = importlib.import_module("cognee.tasks.storage.add_data_points")

DATASET = "egnyte_forget_test"
ROOT = "/Shared/Clients"
# Distinctive tokens so each file maps to exactly one graph entity.
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
        token in str(value).lower() for _id, props in nodes for value in (props or {}).values()
    )


@pytest_asyncio.fixture
async def clean_environment(tmp_path, monkeypatch):
    pytest.importorskip("dlt")
    pytest.importorskip("ladybug")

    monkeypatch.setenv("COGNEE_SKIP_CONNECTION_TEST", "true")
    cognee.config.data_root_directory(str(tmp_path / "data"))
    cognee.config.system_root_directory(str(tmp_path / "system"))
    cognee.config.set_relational_db_config({"db_provider": "sqlite"})

    async def _noop_index(*_args, **_kwargs):
        return None

    monkeypatch.setattr(add_data_points_module, "index_data_points", _noop_index)
    monkeypatch.setattr(add_data_points_module, "index_graph_edges", _noop_index)
    monkeypatch.setattr(LLMGateway, "acreate_structured_output", _mock_structured_output)

    # cognee's own mock embeddings (zero vectors).  The engine is cached, so
    # clear it to make sure this test's setting is the one in use.
    monkeypatch.setenv("MOCK_EMBEDDING", "true")
    create_embedding_engine.cache_clear()

    await cognee.prune.prune_data()
    await cognee.prune.prune_system(metadata=True)
    yield
    await cognee.prune.prune_data()
    await cognee.prune.prune_system(metadata=True)
    create_embedding_engine.cache_clear()


async def _sync(fake):
    await cognee.add(
        egnyte_source(domain="acme", folder_paths=[ROOT], client=fake),
        dataset_name=DATASET,
        primary_key="id",
        write_disposition="merge",
        max_rows_per_table=0,
    )
    await cognee.cognify(datasets=[DATASET])


@pytest.mark.asyncio
async def test_deleting_a_folder_forgets_its_files_graph_content(clean_environment):
    fake = FakeEgnyte()
    fake.put(f"{ROOT}/Alpha/overview.txt", f"{ALPHA} is a company in the logistics sector.")
    fake.put(f"{ROOT}/Bravo/overview.txt", f"{BRAVO} is a company in the finance sector.")

    await _sync(fake)
    assert await _graph_has(ALPHA), "Alpha's entity should be in the graph after ingest"
    assert await _graph_has(BRAVO), "Bravo's entity should be in the graph after ingest"

    fake.delete_folder(f"{ROOT}/Bravo")
    await _sync(fake)

    assert await _graph_has(ALPHA), "Alpha's entity must remain after deleting Bravo's folder"
    assert not await _graph_has(BRAVO), (
        "the deleted folder's entity must be removed from the graph (forget-on-delete), "
        "not just its Data record"
    )
