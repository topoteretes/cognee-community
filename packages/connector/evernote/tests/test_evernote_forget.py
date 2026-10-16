"""Forget-on-delete must remove a note's cognified graph content, not just its
Data record.

Ingests two Evernote notes, cognifies (LLM + embeddings mocked, no live creds),
deletes one note upstream, re-syncs, and asserts the deleted note's extracted
entity is gone from the graph while the surviving note's entity remains.

This is the issue's acceptance criterion — "deleting the source upstream removes
it from the graph on the next sync" — proven at the only layer where it is
meaningful: the graph itself. Regression guard for the orphan-cleanup gap where
``_delete_dlt_orphans`` skipped graph/vector deletion on graph-provenance graphs
(``has_data_related_nodes`` only checks the relational ledger), which left a
forgotten source still retrievable.
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

from cognee_community_connector_evernote.evernote import evernote_source

from .fake_evernote import FakeEvernoteStore

add_data_points_module = importlib.import_module("cognee.tasks.storage.add_data_points")

DATASET = "evernote_forget_test"
TOKEN = "S=s1:U=test:E=test:C=0:P=0cd:A=t:V=2:H=h:F=000:T=web&K=k&S=s"

# Distinctive, unique tokens so each note maps to exactly one graph entity.
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
    for _nid, props in nodes:
        if any(token in str(value).lower() for value in (props or {}).values()):
            return True
    return False


@pytest_asyncio.fixture
async def clean_environment(tmp_path, monkeypatch):
    pytest.importorskip("dlt")
    pytest.importorskip("ladybug")

    monkeypatch.setenv("COGNEE_SKIP_CONNECTION_TEST", "true")

    import dlt

    monkeypatch.setitem(dlt.config, "runtime.pipelines_dir", str(tmp_path / "dlt_pipelines"))
    from cognee.tasks.ingestion.get_dlt_destination import get_dlt_destination

    get_dlt_destination.cache_clear()

    cognee.config.data_root_directory(str(tmp_path / "data"))
    cognee.config.system_root_directory(str(tmp_path / "system"))
    cognee.config.set_relational_db_config({"db_provider": "sqlite"})

    async def _noop_index(*_args, **_kwargs):
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

    get_dlt_destination.cache_clear()
    await cognee.prune.prune_data()
    await cognee.prune.prune_system(metadata=True)


async def _sync_and_cognify(store: FakeEvernoteStore):
    await cognee.add(
        evernote_source(TOKEN, store=store),
        dataset_name=DATASET,
        primary_key="id",
        write_disposition="merge",
        max_rows_per_table=0,
    )
    await cognee.cognify(datasets=[DATASET])


@pytest.mark.asyncio
async def test_deleting_a_note_forgets_its_graph_content(clean_environment):
    store = FakeEvernoteStore(notebooks={"nb-1": "Work"})
    store.add_note(
        "note-a",
        f"<div>{ALPHA} is a company in the logistics sector.</div>",
        notebook_guid="nb-1",
    )
    store.add_note(
        "note-b",
        f"<div>{BRAVO} is an unrelated company in the finance sector.</div>",
        notebook_guid="nb-1",
    )

    await _sync_and_cognify(store)
    assert await _graph_has(ALPHA), "note-a's entity should be in the graph after ingest"
    assert await _graph_has(BRAVO), "note-b's entity should be in the graph after ingest"

    # Delete note-b permanently upstream; the incremental re-sync must forget it.
    store.expunge_note("note-b")
    await _sync_and_cognify(store)

    assert await _graph_has(ALPHA), "surviving note-a entity must remain"
    assert not await _graph_has(BRAVO), "the deleted note's entity must leave the graph"


@pytest.mark.asyncio
async def test_trashing_a_note_forgets_its_graph_content(clean_environment):
    """Moving a note to Trash must forget it just like a permanent delete."""
    store = FakeEvernoteStore()
    store.add_note("note-a", f"<div>{ALPHA} is a company in the logistics sector.</div>")
    store.add_note("note-b", f"<div>{BRAVO} is an unrelated company in the finance sector.</div>")

    await _sync_and_cognify(store)
    assert await _graph_has(BRAVO)

    store.trash_note("note-b")
    await _sync_and_cognify(store)

    assert not await _graph_has(BRAVO), "a trashed note's entity must leave the graph"
    assert await _graph_has(ALPHA), "surviving entity must remain"


@pytest.mark.asyncio
async def test_editing_a_note_updates_its_graph_content(clean_environment):
    """An edited note is re-ingested under a new content hash, so its new text wins."""
    store = FakeEvernoteStore()
    store.add_note("note-a", f"<div>{ALPHA} started in the logistics sector.</div>")
    await _sync_and_cognify(store)
    assert await _graph_has(ALPHA)

    # Replace the body with different prose. The old version is superseded by the
    # new content hash, and the old version's orphan is cleaned up.
    store.edit_note("note-a", "<div>This note no longer mentions any company at all.</div>")
    await _sync_and_cognify(store)

    assert not await _graph_has(ALPHA), "the superseded version's entity must be gone"
