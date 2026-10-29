"""End-to-end integration test: Evernote connector through the real cognee.add() pipeline.

Uses offline SQLite/local stores with fake Evernote API clients (no network calls,
no live credentials, no LLM calls).
"""

from types import SimpleNamespace

import cognee
import pytest
import pytest_asyncio
from cognee.modules.data.methods import get_authorized_existing_datasets
from cognee.modules.data.methods.get_dataset_data import get_dataset_data
from cognee.modules.users.methods import get_default_user
from evernote.edam.notestore.ttypes import SyncChunk, SyncState

from cognee_community_connector_evernote import evernote_source

DATASET_NAME = "evernote_integration_test"


class FakeEvernoteClient:
    def __init__(self, chunks=None, note_contents=None, update_count=None):
        self.chunks = list(chunks or [])
        self.note_contents = dict(note_contents or {})
        self.update_count = update_count
        self.get_filtered_sync_chunk_calls = []

    def get_user_id(self):
        return 12345

    def get_sync_state(self):
        count = self.update_count
        if count is None and self.chunks:
            count = getattr(self.chunks[0], "updateCount", 1000) or 1000
        elif count is None:
            count = 1000
        return SyncState(
            currentTime=1609459200000,
            fullSyncBefore=0,
            updateCount=count,
        )

    def get_filtered_sync_chunk(self, after_usn, max_entries, filter_obj):
        self.get_filtered_sync_chunk_calls.append((after_usn, max_entries, filter_obj))
        if self.chunks:
            return self.chunks.pop(0)
        return SyncChunk(chunkHighUSN=None)

    def get_note_content(self, guid):
        return self.note_contents.get(
            guid, f"<en-note><div>Default content for {guid}</div></en-note>"
        )


@pytest_asyncio.fixture
async def persistent_environment(tmp_path, monkeypatch):
    """Offline Cognee environment with isolated SQLite, local files, and no LLM calls."""
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

    await cognee.prune.prune_data()
    await cognee.prune.prune_system(metadata=True)
    await setup()

    yield tmp_path

    Container()[PipelineContext].deactivate()
    await cognee.prune.prune_data()
    await cognee.prune.prune_system(metadata=True)


def _get_metadata(d):
    """Extract metadata dictionary, supporting both system_metadata and external_metadata."""
    if isinstance(d.system_metadata, dict) and "source" in d.system_metadata:
        return d.system_metadata
    if isinstance(d.external_metadata, dict) and "source" in d.external_metadata:
        return d.external_metadata
    return d.system_metadata if isinstance(d.system_metadata, dict) else (d.external_metadata or {})


async def _get_evernote_dataset_data(dataset_name: str):
    """Retrieve all ingested Data records tagged with source='evernote'."""
    user = await get_default_user()
    datasets = await get_authorized_existing_datasets(
        user=user, permission_type="write", datasets=[dataset_name]
    )
    if not datasets:
        return []
    all_data = await get_dataset_data(datasets[0].id)
    return [d for d in all_data if _get_metadata(d).get("source") == "evernote"]


@pytest.mark.asyncio
async def test_evernote_ingestion_lifecycle(persistent_environment):
    """Full lifecycle test: initial add, deduplication, update, and deletion propagation."""
    # 1. Initial ingestion: 3 active notes
    nb = SimpleNamespace(guid="nb-1", name="Engineering")
    note_a = SimpleNamespace(
        guid="noteA",
        title="Note A Title",
        notebookGuid="nb-1",
        tagGuids=[],
        active=True,
        created=1609459200000,
        updated=1609459200000,
    )
    note_b = SimpleNamespace(
        guid="noteB",
        title="Note B Title",
        notebookGuid="nb-1",
        tagGuids=[],
        active=True,
        created=1609459200000,
        updated=1609459200000,
    )
    note_c = SimpleNamespace(
        guid="noteC",
        title="Note C Title",
        notebookGuid="nb-1",
        tagGuids=[],
        active=True,
        created=1609459200000,
        updated=1609459200000,
    )

    contents = {
        "noteA": "<en-note><div>Alice architecture document</div></en-note>",
        "noteB": "<en-note><div>Bob backend runbook</div></en-note>",
        "noteC": "<en-note><div>Carol CI/CD pipelines</div></en-note>",
    }

    chunk_initial = SyncChunk(
        chunkHighUSN=10,
        updateCount=10,
        notebooks=[nb],
        notes=[note_a, note_b, note_c],
    )
    client = FakeEvernoteClient(chunks=[chunk_initial], note_contents=contents)

    source1 = evernote_source(token="fake-token", client=client)
    await cognee.add(source1, dataset_name=DATASET_NAME, write_disposition="merge")

    # Assert 1: Data records created with source == "evernote"
    initial_data = await _get_evernote_dataset_data(DATASET_NAME)
    assert len(initial_data) == 3
    initial_ids_by_guid = {_get_metadata(d)["external_id"]: d.id for d in initial_data}
    assert set(initial_ids_by_guid.keys()) == {"noteA", "noteB", "noteC"}
    for d in initial_data:
        assert _get_metadata(d)["source"] == "evernote"

    # Assert 2: Re-running with unchanged notes creates no duplicates (stable data_ids)
    chunk_unchanged = SyncChunk(
        chunkHighUSN=10,
        updateCount=10,
        notebooks=[nb],
        notes=[note_a, note_b, note_c],
    )
    client.chunks = [chunk_unchanged]
    source2 = evernote_source(token="fake-token", client=client)
    await cognee.add(source2, dataset_name=DATASET_NAME, write_disposition="merge")

    second_data = await _get_evernote_dataset_data(DATASET_NAME)
    assert len(second_data) == 3
    second_ids_by_guid = {_get_metadata(d)["external_id"]: d.id for d in second_data}
    assert second_ids_by_guid == initial_ids_by_guid

    # Assert 3: An edited note updates its Data record
    contents["noteA"] = "<en-note><div>Alice architecture document REVISED v2</div></en-note>"
    note_a_edited = SimpleNamespace(
        guid="noteA",
        title="Note A Title",
        notebookGuid="nb-1",
        tagGuids=[],
        active=True,
        created=1609459200000,
        updated=1612137600000,
    )
    chunk_edited = SyncChunk(
        chunkHighUSN=15,
        updateCount=15,
        notebooks=[nb],
        notes=[note_a_edited, note_b, note_c],
    )
    client.chunks = [chunk_edited]
    source3 = evernote_source(token="fake-token", client=client)
    await cognee.add(source3, dataset_name=DATASET_NAME, write_disposition="merge")

    third_data = await _get_evernote_dataset_data(DATASET_NAME)
    assert len(third_data) == 3
    third_ids_by_guid = {_get_metadata(d)["external_id"]: d.id for d in third_data}
    # noteA was edited -> its content-hash derived data_id changed
    assert third_ids_by_guid["noteA"] != initial_ids_by_guid["noteA"]
    # noteB and noteC were unchanged -> same data_id
    assert third_ids_by_guid["noteB"] == initial_ids_by_guid["noteB"]
    assert third_ids_by_guid["noteC"] == initial_ids_by_guid["noteC"]

    # Assert 4: A trashed or expunged note on the next run removes its Data record
    # noteB becomes trashed (active=False), noteC is expunged
    note_b_trashed = SimpleNamespace(
        guid="noteB",
        title="Note B Title",
        notebookGuid="nb-1",
        tagGuids=[],
        active=False,
    )
    chunk_deletion = SyncChunk(
        chunkHighUSN=20,
        updateCount=20,
        notebooks=[nb],
        notes=[note_a_edited, note_b_trashed],
        expungedNotes=["noteC"],
    )
    client.chunks = [chunk_deletion]
    source4 = evernote_source(token="fake-token", client=client)
    await cognee.add(source4, dataset_name=DATASET_NAME, write_disposition="merge")

    final_data = await _get_evernote_dataset_data(DATASET_NAME)
    final_guids = {_get_metadata(d)["external_id"] for d in final_data}
    assert final_guids == {"noteA"}
    assert "noteB" not in final_guids
    assert "noteC" not in final_guids
    assert len(final_data) == 1


@pytest.mark.asyncio
async def test_evernote_incremental_ingestion_with_cursor(persistent_environment):
    """Incremental sync across cognee.add runs: run 1 ingests 3 notes, run 2 edits/trashes notes."""
    dataset = "evernote_cursor_dataset"
    nb = SimpleNamespace(guid="nb-1", name="Engineering")
    note1 = SimpleNamespace(
        guid="n1",
        title="Note 1",
        notebookGuid="nb-1",
        tagGuids=[],
        active=True,
        created=1609459200000,
        updated=1609459200000,
    )
    note2 = SimpleNamespace(
        guid="n2",
        title="Note 2",
        notebookGuid="nb-1",
        tagGuids=[],
        active=True,
        created=1609459200000,
        updated=1609459200000,
    )
    note3 = SimpleNamespace(
        guid="n3",
        title="Note 3",
        notebookGuid="nb-1",
        tagGuids=[],
        active=True,
        created=1609459200000,
        updated=1609459200000,
    )

    contents = {
        "n1": "<en-note><div>Body 1</div></en-note>",
        "n2": "<en-note><div>Body 2</div></en-note>",
        "n3": "<en-note><div>Body 3</div></en-note>",
    }

    # Run 1: initial ingestion of 3 notes
    chunk1 = SyncChunk(
        chunkHighUSN=10,
        updateCount=10,
        notebooks=[nb],
        notes=[note1, note2, note3],
    )
    client = FakeEvernoteClient(chunks=[chunk1], note_contents=contents, update_count=10)
    source1 = evernote_source(token="fake-token", client=client)
    await cognee.add(source1, dataset_name=dataset, write_disposition="merge")

    # Assert 3 data records created on Run 1
    data1 = await _get_evernote_dataset_data(dataset)
    assert len(data1) == 3
    assert {_get_metadata(d)["external_id"] for d in data1} == {"n1", "n2", "n3"}
    assert client.get_filtered_sync_chunk_calls[0][0] == 0

    # Run 2: incremental run editing note1 and trashing note2
    contents["n1"] = "<en-note><div>Body 1 Updated</div></en-note>"
    note1_edited = SimpleNamespace(
        guid="n1",
        title="Note 1 Updated",
        notebookGuid="nb-1",
        tagGuids=[],
        active=True,
        created=1609459200000,
        updated=1609500000000,
    )
    note2_trashed = SimpleNamespace(
        guid="n2",
        title="Note 2",
        notebookGuid="nb-1",
        tagGuids=[],
        active=False,
    )

    chunk2 = SyncChunk(
        chunkHighUSN=20,
        updateCount=20,
        notes=[note1_edited, note2_trashed],
    )
    client.chunks = [chunk2]
    client.update_count = 20

    source2 = evernote_source(token="fake-token", client=client)
    await cognee.add(source2, dataset_name=dataset, write_disposition="merge")

    # Assert that the fake api saw afterUSN > 0 on run 2
    assert len(client.get_filtered_sync_chunk_calls) == 2
    after_usn_run2 = client.get_filtered_sync_chunk_calls[1][0]
    assert after_usn_run2 == 10
    assert after_usn_run2 > 0

    # Assert Data records: n1 updated, n2 removed, n3 retained
    data2 = await _get_evernote_dataset_data(dataset)
    guids_run2 = {_get_metadata(d)["external_id"] for d in data2}
    assert guids_run2 == {"n1", "n3"}
    assert "n2" not in guids_run2
