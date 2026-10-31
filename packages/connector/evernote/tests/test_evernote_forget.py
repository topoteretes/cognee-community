"""Forget-on-delete must remove a note's cognified graph content, not just its Data record.

Ingests two Evernote notes (noteA and noteB), cognifies them with the LLM and embeddings
mocked, then trashes noteB (active=False tombstone) or expunges it, and verifies that:
  - noteB's Data record is gone.
  - noteB's specific graph entity is absent from the graph.
  - noteA's Data record survives.
  - noteA's specific graph entity survives.
  - The shared entity (present in both notes) survives because noteA still references it.

The LLM mock maps each note's distinctive token to exactly one named entity. A shared token
appears in both notes and must therefore remain after noteB is forgotten.

Vector-store assertions: The mock patches ``index_data_points`` and ``index_graph_edges``
with no-ops, so no real vectors are written to LanceDB and vector-level assertions are not
possible offline. The assertion we *can* make is that the graph nodes for deleted-note
entities are absent (graph is the authoritative store for semantic content; the vector store
merely indexes those nodes). If a real vector store is wired in future, a dedicated vector
search test should be added.

Rate-limit resumable sync regression: The test
``test_interrupted_batch_resumes_cleanly_in_cognee`` in ``test_evernote_ingestion.py``
covers the scenario where ``max_notes_per_run`` limits the first run to 5 of 10 notes and
the second run loads the remaining 5, ending with all 10 Data records and no duplicates.
That test exercises the resumable-sync (partial-state) feature end-to-end through
``cognee.add``.
"""

import importlib
from types import SimpleNamespace

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
from evernote.edam.notestore.ttypes import SyncChunk, SyncState

from cognee_community_connector_evernote import evernote_source

add_data_points_module = importlib.import_module("cognee.tasks.storage.add_data_points")

DATASET = "evernote_forget_test"

# Tokens that each note carries.  The LLM mock yields a graph node named after
# whichever token it finds in the chunk text.
ALPHA = "Alphacorp"  # entity unique to noteA
BRAVO = "Bravocorp"  # entity unique to noteB
SHARED = "Sharedcorp"  # entity present in BOTH notes; must survive after noteB is deleted


# ---------------------------------------------------------------------------
# Fake Evernote client
# ---------------------------------------------------------------------------


class FakeEvernoteClient:
    def __init__(self, chunks=None, note_contents=None, update_count=None):
        self.chunks = list(chunks or [])
        self.note_contents = dict(note_contents or {})
        self.update_count = update_count

    def get_user_id(self):
        return 99

    def get_sync_state(self):
        count = self.update_count
        if count is None and self.chunks:
            count = getattr(self.chunks[0], "updateCount", 1000) or 1000
        elif count is None:
            count = 1000
        return SyncState(
            currentTime=1_700_000_000_000,
            fullSyncBefore=0,
            updateCount=count,
        )

    def get_filtered_sync_chunk(self, after_usn, max_entries, filter_obj):
        if self.chunks:
            return self.chunks.pop(0)
        return SyncChunk(chunkHighUSN=None)

    def get_note_content(self, guid):
        return self.note_contents.get(
            guid, f"<en-note><div>Default content for {guid}</div></en-note>"
        )


# ---------------------------------------------------------------------------
# LLM mock: map known tokens to graph entities
# ---------------------------------------------------------------------------

TOKENS = (ALPHA, BRAVO, SHARED)


async def _mock_structured_output(
    text_input=None, system_prompt=None, response_model=str, **_kwargs
):
    """Produce exactly one graph entity per distinctive token found in the chunk text."""
    from cognee.shared.data_models import KnowledgeGraph, SummarizedContent
    from cognee.shared.data_models import Node as KGNode

    if response_model is str:
        return "Mocked answer."
    if response_model == SummarizedContent:
        return SummarizedContent(summary="Mock summary", description="Mock summary")
    if response_model == KnowledgeGraph:
        found = [t for t in TOKENS if text_input and t in text_input]
        nodes = [
            KGNode(id=name, name=name, type="Company", description=f"{name} entity")
            for name in found
        ]
        return KnowledgeGraph(nodes=nodes, edges=[])
    return response_model()


# ---------------------------------------------------------------------------
# Graph helper
# ---------------------------------------------------------------------------


async def _graph_has(token: str) -> bool:
    """Return True if the graph contains any node whose properties mention *token*."""
    nodes, _ = await (await get_graph_engine()).get_graph_data()
    needle = token.lower()
    return any(
        needle in str(value).lower() for _nid, props in nodes for value in (props or {}).values()
    )


# ---------------------------------------------------------------------------
# Data-record helper (same pattern as test_evernote_ingestion.py)
# ---------------------------------------------------------------------------


def _get_metadata(d):
    if isinstance(d.system_metadata, dict) and "source" in d.system_metadata:
        return d.system_metadata
    if isinstance(d.external_metadata, dict) and "source" in d.external_metadata:
        return d.external_metadata
    return d.system_metadata if isinstance(d.system_metadata, dict) else (d.external_metadata or {})


async def _data_guids(dataset_name: str) -> set[str]:
    user = await get_default_user()
    datasets = await get_authorized_existing_datasets(
        user=user, permission_type="write", datasets=[dataset_name]
    )
    if not datasets:
        return set()
    all_data = await get_dataset_data(datasets[0].id)
    return {
        _get_metadata(d).get("external_id")
        for d in all_data
        if _get_metadata(d).get("source") == "evernote"
    }


# ---------------------------------------------------------------------------
# Fixture: offline cognee env with mocked LLM + embeddings
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def clean_environment(tmp_path, monkeypatch):
    """Isolated cognee env with SQLite/Ladybug/LanceDB and no real LLM or API calls."""
    pytest.importorskip("dlt")
    pytest.importorskip("ladybug")

    from cognee.context_global_variables import graph_db_config, vector_db_config
    from cognee.infrastructure.databases.graph.get_graph_engine import _create_graph_engine
    from cognee.infrastructure.databases.relational.create_relational_engine import (
        create_relational_engine,
    )
    from cognee.infrastructure.databases.vector.create_vector_engine import _create_vector_engine
    from cognee.modules.engine.operations.setup import setup
    from cognee.tasks.ingestion.get_dlt_destination import get_dlt_destination
    from dlt.common.configuration.container import Container
    from dlt.common.pipeline import PipelineContext

    Container()[PipelineContext].deactivate()
    for key, value in {
        "COGNEE_SKIP_CONNECTION_TEST": "true",
        "ENABLE_BACKEND_ACCESS_CONTROL": "true",
        "GRAPH_DATASET_DATABASE_HANDLER": "ladybug",
        "VECTOR_DATASET_DATABASE_HANDLER": "lancedb",
        "LLM_API_KEY": "sk-offline",
        "MOCK_EMBEDDING": "true",
        "EMBEDDING_PROVIDER": "openai",
        "EMBEDDING_MODEL": "openai/text-embedding-3-large",
        "EMBEDDING_DIMENSIONS": "384",
        "DLT_DATA_DIR": str(tmp_path / "dlt"),
        "PIPELINES_DIR": str(tmp_path / "dlt" / "pipelines"),
    }.items():
        monkeypatch.setenv(key, value)

    for factory in (
        _create_graph_engine,
        _create_vector_engine,
        create_relational_engine,
        get_dlt_destination,
    ):
        factory.cache_clear()

    graph_db_config.set(None)
    vector_db_config.set(None)
    cognee.config.set_graph_db_config(
        {"graph_database_provider": "ladybug", "graph_dataset_database_handler": "ladybug"}
    )
    cognee.config.set_vector_db_config(
        {"vector_db_provider": "lancedb", "vector_dataset_database_handler": "lancedb"}
    )
    cognee.config.set_relational_db_config({"db_provider": "sqlite"})
    cognee.config.set_migration_db_config({"migration_db_provider": "sqlite"})
    cognee.config.system_root_directory(str(tmp_path / "system"))
    cognee.config.data_root_directory(str(tmp_path / "data"))
    cognee.config.set_vector_db_url(str(tmp_path / "system" / "databases" / "vectors"))

    # Silence the vector indexing side-effect so no real embeddings are needed
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
    await setup()

    yield

    Container()[PipelineContext].deactivate()
    await cognee.prune.prune_data()
    await cognee.prune.prune_system(metadata=True)


# ---------------------------------------------------------------------------
# Shared sync helper
# ---------------------------------------------------------------------------


async def _sync(client, dataset=DATASET):
    await cognee.add(
        evernote_source(token="fake-token", service=client),
        dataset_name=dataset,
        write_disposition="merge",
        max_rows_per_table=0,
    )
    await cognee.cognify(datasets=[dataset])


# ---------------------------------------------------------------------------
# Test 1: trashed note (active=False tombstone)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_trashed_note_forgotten_from_graph(clean_environment):
    """Trashing noteB must remove its entity from the graph; noteA and SHARED survive.

    Scenario:
      - noteA carries ALPHA (unique) and SHARED.
      - noteB carries BRAVO (unique) and SHARED.
    After noteB is trashed (active=False) and re-synced:
      - ALPHA and SHARED must remain in the graph.
      - BRAVO must be gone from the graph.
      - noteA's Data record must remain; noteB's must be gone.

    Vector-store note: index_data_points and index_graph_edges are patched to no-ops so no
    LanceDB entries are written in this offline test; vector assertions are therefore not
    possible here and are explicitly excluded rather than silently omitted.
    """
    nb = SimpleNamespace(guid="nb-1", name="Engineering")
    note_a = SimpleNamespace(
        guid="noteA",
        title="Note A",
        notebookGuid="nb-1",
        tagGuids=[],
        active=True,
        updateSequenceNum=1,
        created=1_609_459_200_000,
        updated=1_609_459_200_000,
    )
    note_b = SimpleNamespace(
        guid="noteB",
        title="Note B",
        notebookGuid="nb-1",
        tagGuids=[],
        active=True,
        updateSequenceNum=2,
        created=1_609_459_200_000,
        updated=1_609_459_200_000,
    )
    contents = {
        # noteA: distinct entity ALPHA + shared entity SHARED
        "noteA": f"<en-note><div>{ALPHA} and {SHARED} are partners.</div></en-note>",
        # noteB: distinct entity BRAVO + shared entity SHARED
        "noteB": f"<en-note><div>{BRAVO} and {SHARED} are rivals.</div></en-note>",
    }

    # Run 1: ingest both notes
    chunk1 = SyncChunk(
        chunkHighUSN=10,
        updateCount=10,
        notebooks=[nb],
        notes=[note_a, note_b],
    )
    client = FakeEvernoteClient(chunks=[chunk1], note_contents=contents, update_count=10)
    await _sync(client)

    # Verify both notes and all three entities are present
    assert await _data_guids(DATASET) == {"noteA", "noteB"}, (
        "both notes should be ingested after run 1"
    )
    assert await _graph_has(ALPHA), f"{ALPHA} should be in graph after initial ingest"
    assert await _graph_has(BRAVO), f"{BRAVO} should be in graph after initial ingest"
    assert await _graph_has(SHARED), f"{SHARED} should be in graph after initial ingest"

    # Run 2: noteB is trashed (active=False); noteA is unchanged
    note_b_trashed = SimpleNamespace(
        guid="noteB",
        title="Note B",
        notebookGuid="nb-1",
        tagGuids=[],
        active=False,
        updateSequenceNum=11,
    )
    chunk2 = SyncChunk(
        chunkHighUSN=20,
        updateCount=20,
        notebooks=[nb],
        notes=[note_b_trashed],
    )
    client2 = FakeEvernoteClient(chunks=[chunk2], note_contents=contents, update_count=20)
    await _sync(client2)

    # Data-record assertions
    final_guids = await _data_guids(DATASET)
    assert "noteA" in final_guids, "noteA's Data record must survive after noteB is trashed"
    assert "noteB" not in final_guids, "noteB's Data record must be gone after it is trashed"

    # Graph assertions
    assert await _graph_has(ALPHA), (
        f"{ALPHA} (entity unique to noteA) must remain in graph after noteB is trashed"
    )
    assert await _graph_has(SHARED), (
        f"{SHARED} (shared entity) must remain in graph because noteA still references it"
    )
    assert not await _graph_has(BRAVO), (
        f"{BRAVO} (entity unique to noteB) must be removed from graph when noteB is trashed"
    )


# ---------------------------------------------------------------------------
# Test 2: expunged note (expungedNotes tombstone)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_expunged_note_forgotten_from_graph(clean_environment):
    """Expunging noteB must remove its entity from the graph; noteA and SHARED survive.

    Same scenario as the trashed-note test but the server sends noteB's GUID in
    expungedNotes rather than setting active=False. The expected outcome is identical:
    only BRAVO leaves the graph; ALPHA and SHARED remain.

    Vector-store note: same as above — index_data_points/index_graph_edges are no-ops,
    so vector-level assertions are not possible in this offline test.
    """
    nb = SimpleNamespace(guid="nb-1", name="Engineering")
    note_a = SimpleNamespace(
        guid="noteA",
        title="Note A",
        notebookGuid="nb-1",
        tagGuids=[],
        active=True,
        updateSequenceNum=1,
        created=1_609_459_200_000,
        updated=1_609_459_200_000,
    )
    note_b = SimpleNamespace(
        guid="noteB",
        title="Note B",
        notebookGuid="nb-1",
        tagGuids=[],
        active=True,
        updateSequenceNum=2,
        created=1_609_459_200_000,
        updated=1_609_459_200_000,
    )
    contents = {
        "noteA": f"<en-note><div>{ALPHA} and {SHARED} are partners.</div></en-note>",
        "noteB": f"<en-note><div>{BRAVO} and {SHARED} are rivals.</div></en-note>",
    }

    # Run 1: ingest both notes
    chunk1 = SyncChunk(
        chunkHighUSN=10,
        updateCount=10,
        notebooks=[nb],
        notes=[note_a, note_b],
    )
    client = FakeEvernoteClient(chunks=[chunk1], note_contents=contents, update_count=10)
    dataset = "evernote_forget_expunged_test"
    await _sync(client, dataset=dataset)

    assert await _data_guids(dataset) == {"noteA", "noteB"}, (
        "both notes should be ingested after run 1"
    )
    assert await _graph_has(ALPHA), f"{ALPHA} should be in graph after initial ingest"
    assert await _graph_has(BRAVO), f"{BRAVO} should be in graph after initial ingest"
    assert await _graph_has(SHARED), f"{SHARED} should be in graph after initial ingest"

    # Run 2: noteB is expunged (listed in expungedNotes)
    chunk2 = SyncChunk(
        chunkHighUSN=20,
        updateCount=20,
        expungedNotes=["noteB"],
    )
    client2 = FakeEvernoteClient(chunks=[chunk2], note_contents=contents, update_count=20)
    await _sync(client2, dataset=dataset)

    # Data-record assertions
    final_guids = await _data_guids(dataset)
    assert "noteA" in final_guids, "noteA's Data record must survive after noteB is expunged"
    assert "noteB" not in final_guids, "noteB's Data record must be gone after it is expunged"

    # Graph assertions
    assert await _graph_has(ALPHA), (
        f"{ALPHA} (entity unique to noteA) must remain in graph after noteB is expunged"
    )
    assert await _graph_has(SHARED), (
        f"{SHARED} (shared entity) must remain in graph because noteA still references it"
    )
    assert not await _graph_has(BRAVO), (
        f"{BRAVO} (entity unique to noteB) must be removed from graph when noteB is expunged"
    )
