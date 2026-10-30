"""Evernote connector demo — ingest Evernote notes into cognee memory.

Demonstrates pulling Evernote notes (with notebook and tag metadata) into cognee,
with incremental synchronization via sync chunks and forget-on-delete.

Usage:
    # 1. Live mode (requires EVERNOTE_TOKEN and an LLM API key):
    export EVERNOTE_TOKEN="your-evernote-token"
    export LLM_API_KEY="sk-..."
    python examples/example.py

    # 2. Fake / offline mode (isolated in-memory demo, no credentials required):
    python examples/example.py --fake
"""

import asyncio
import os
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

import cognee
from cognee.modules.data.methods import get_authorized_existing_datasets
from cognee.modules.data.methods.get_dataset_data import get_dataset_data
from cognee.modules.users.methods import get_default_user
from evernote.edam.notestore.ttypes import SyncChunk, SyncState

from cognee_community_connector_evernote import evernote_source

DATASET_NAME = "evernote_demo"


class FakeEvernoteClient:
    """In-memory fake Evernote client for offline demonstration."""

    def __init__(self, chunks=None, note_contents=None, update_count=None):
        self.chunks = list(chunks or [])
        self.note_contents = dict(note_contents or {})
        self.update_count = update_count

    def get_user_id(self) -> int:
        return 12345

    def get_sync_state(self) -> SyncState:
        count = self.update_count
        if count is None and self.chunks:
            count = getattr(self.chunks[0], "updateCount", 1000) or 1000
        elif count is None:
            count = 1000
        return SyncState(currentTime=1609459200000, fullSyncBefore=0, updateCount=count)

    def get_filtered_sync_chunk(
        self, after_usn: int, max_entries: int, filter_obj: object
    ) -> SyncChunk:
        if self.chunks:
            return self.chunks.pop(0)
        return SyncChunk(chunkHighUSN=None)

    def get_note_content(self, guid: str) -> str:
        return self.note_contents.get(
            guid, f"<en-note><div>Default content for {guid}</div></en-note>"
        )


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


async def run_fake_demo() -> None:
    """Run an isolated, offline demonstration showing ingestion and forget-on-delete."""
    print("=== Running in fake mode (in-memory test data, offline) ===")

    with tempfile.TemporaryDirectory(prefix="cognee_evernote_fake_") as temp_dir_str:
        tmp_path = Path(temp_dir_str)

        # 1. Isolation setup matching persistent_environment in test_evernote_ingestion.py
        from cognee.context_global_variables import graph_db_config, vector_db_config
        from cognee.infrastructure.databases.graph.get_graph_engine import _create_graph_engine
        from cognee.infrastructure.databases.relational.create_relational_engine import (
            create_relational_engine,
        )
        from cognee.infrastructure.databases.vector.create_vector_engine import (
            _create_vector_engine,
        )
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
            os.environ[key] = value

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

        try:
            # Scenario 1: Initial Ingestion of 2 notes
            print("\n--- Scenario 1: Initial ingestion of 2 active notes ---")
            nb = SimpleNamespace(guid="nb-1", name="Engineering")
            note1 = SimpleNamespace(
                guid="note-1",
                title="Architecture Guidelines",
                notebookGuid="nb-1",
                tagGuids=[],
                active=True,
                created=1609459200000,
                updated=1609459200000,
            )
            note2 = SimpleNamespace(
                guid="note-2",
                title="Deployment Runbook",
                notebookGuid="nb-1",
                tagGuids=[],
                active=True,
                created=1609459200000,
                updated=1609459200000,
            )
            contents = {
                "note-1": (
                    "<en-note><div>Architecture and guidelines for building "
                    "reliable microservices.</div></en-note>"
                ),
                "note-2": (
                    "<en-note><div>Deployment runbook for production infrastructure, "
                    "health checks, and rollbacks.</div></en-note>"
                ),
            }

            client = FakeEvernoteClient(
                chunks=[
                    SyncChunk(
                        chunkHighUSN=10,
                        updateCount=10,
                        notebooks=[nb],
                        notes=[note1, note2],
                    )
                ],
                note_contents=contents,
                update_count=10,
            )

            source1 = evernote_source(token="fake-token", client=client)
            await cognee.add(
                source1,
                dataset_name=DATASET_NAME,
                write_disposition="merge",
                max_rows_per_table=0,
            )

            items = await _get_evernote_dataset_data(DATASET_NAME)
            print(f"Ingested {len(items)} note(s) into dataset '{DATASET_NAME}':")
            for item in items:
                meta = _get_metadata(item)
                title = meta.get("title") or item.name
                ext_id = meta.get("external_id")
                print(f"  - Title: {title!r} (GUID: {ext_id})")

            print("[Note: Semantic search requires a real embedding model and LLM key.]")

            # Scenario 2: Trashed note propagates deletion (forget-on-delete)
            print("\n--- Scenario 2: Trashing a note in Evernote propagates deletion to cognee ---")
            note2_trashed = SimpleNamespace(
                guid="note-2",
                title="Deployment Runbook",
                notebookGuid="nb-1",
                tagGuids=[],
                active=False,
            )
            client.chunks = [
                SyncChunk(
                    chunkHighUSN=20,
                    updateCount=20,
                    notebooks=[nb],
                    notes=[note1, note2_trashed],
                )
            ]
            client.update_count = 20

            source2 = evernote_source(token="fake-token", client=client)
            await cognee.add(
                source2,
                dataset_name=DATASET_NAME,
                write_disposition="merge",
                max_rows_per_table=0,
            )

            remaining_items = await _get_evernote_dataset_data(DATASET_NAME)
            print(f"Remaining {len(remaining_items)} note(s) in dataset '{DATASET_NAME}':")
            for item in remaining_items:
                meta = _get_metadata(item)
                title = meta.get("title") or item.name
                ext_id = meta.get("external_id")
                print(f"  - Title: {title!r} (GUID: {ext_id})")

            remaining_guids = {_get_metadata(d).get("external_id") for d in remaining_items}
            if "note-2" not in remaining_guids:
                print("Confirmed: Trashed note 'note-2' was removed from cognee memory.")

        finally:
            Container()[PipelineContext].deactivate()
            await cognee.prune.prune_data()
            await cognee.prune.prune_system(metadata=True)

    print("\n=== Fake mode completed successfully ===")


async def run_live(token: str) -> None:
    """Run live ingestion and search against an Evernote account."""
    print("=== Running with live Evernote token ===")
    source = evernote_source(token=token)

    print("1. Ingesting Evernote notes into cognee (merge disposition) ...")
    await cognee.add(
        source,
        dataset_name=DATASET_NAME,
        write_disposition="merge",
        max_rows_per_table=0,
    )

    print("2. Cognifying dataset ...")
    await cognee.cognify(datasets=[DATASET_NAME])

    print("3. Searching dataset ...")
    results = await cognee.search(
        query_text="Summarize my notes.",
        query_type=cognee.SearchType.CHUNKS,
        datasets=[DATASET_NAME],
    )
    print(f"Search completed: received {len(results)} dataset result(s).")
    for r in results:
        print("Result:", r)

    print("=== Live sync completed successfully ===")


async def main() -> None:
    if "--fake" in sys.argv:
        await run_fake_demo()
        return

    token = os.environ.get("EVERNOTE_TOKEN")
    if not token:
        print("Error: EVERNOTE_TOKEN environment variable is not set.", file=sys.stderr)
        print("\nTo run with a live Evernote account:", file=sys.stderr)
        print("    export EVERNOTE_TOKEN='your-evernote-token'", file=sys.stderr)
        print("    export LLM_API_KEY='sk-...'", file=sys.stderr)
        print("    python examples/example.py", file=sys.stderr)
        print(
            "\nTo run without Evernote credentials using an in-memory test client:",
            file=sys.stderr,
        )
        print("    python examples/example.py --fake", file=sys.stderr)
        sys.exit(1)

    await run_live(token=token)


if __name__ == "__main__":
    asyncio.run(main())
