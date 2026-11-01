"""Known upstream cleanup dependency: acknowledge no success with an orphan vector."""

import importlib.util
import json
from pathlib import Path

import cognee
import pytest
import pytest_asyncio
from cognee.infrastructure.databases.graph import get_graph_engine
from cognee.infrastructure.databases.vector import get_vector_engine_async
from cognee.infrastructure.databases.vector.lancedb.LanceDBAdapter import LanceDBAdapter

from cognee_community_connector_read_ai import VerificationError, read_ai_source, sync_read_ai

_spec = importlib.util.spec_from_file_location(
    "read_ai_dependency_offline", Path(__file__).parents[1] / "examples" / "offline.py"
)
offline = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(offline)


@pytest_asyncio.fixture
async def local_storage(tmp_path):
    offline.configure_storage(tmp_path)
    models = offline.SyntheticModels().install()
    await cognee.prune.prune_data()
    await cognee.prune.prune_system(metadata=True)
    try:
        yield models
    finally:
        await cognee.prune.prune_data()
        await cognee.prune.prune_system(metadata=True)
        models.close()


@pytest.mark.asyncio
async def test_orphaned_edge_type_vector_blocks_ack_and_requires_core_repair(
    local_storage, tmp_path, monkeypatch
):
    raw = offline.meeting(1, f"{offline.ALPHA} and {offline.SHARED} approved logistics.")
    provider = offline.SyntheticReadAI([raw])
    source = read_ai_source(
        "cleanup-dependency",
        token_provider=provider,
        client=provider.client,
        state_dir=tmp_path / "connector",
    )
    dataset_name = "read_ai_cleanup_dependency"
    original_delete = LanceDBAdapter.delete_data_points
    failed_ids = []

    async def fail_after_graph_deletion(engine, collection_name, data_point_ids):
        if collection_name == "EdgeType_relationship_name" and not failed_ids:
            graph = await get_graph_engine()
            # Main provenance deletion removes vectors before graph nodes. Target
            # only the later nonfatal orphan EdgeType cleanup after graph removal.
            if not await graph.get_node_delete_data(list(data_point_ids)):
                failed_ids.extend(map(str, data_point_ids))
                raise RuntimeError("synthetic one-time EdgeType vector deletion failure")
        return await original_delete(engine, collection_name, data_point_ids)

    try:
        await sync_read_ai(source, dataset_name=dataset_name)
        journal = next((tmp_path / "connector" / "connections").glob("*.json"))
        checkpoint = json.loads(journal.read_text())["last_success"]
        assert checkpoint is not None
        provider.meetings.clear()
        monkeypatch.setattr(LanceDBAdapter, "delete_data_points", fail_after_graph_deletion)
        with pytest.raises(VerificationError, match="EdgeType vector"):
            await sync_read_ai(source, dataset_name=dataset_name)
        assert failed_ids, "the real core post-graph-delete cleanup must reach the injection"
        assert json.loads(journal.read_text())["last_success"] == checkpoint
        assert json.loads(journal.read_text())["attempt"] is not None
        monkeypatch.setattr(LanceDBAdapter, "delete_data_points", original_delete)
        vector = await get_vector_engine_async()
        assert await vector.retrieve("EdgeType_relationship_name", failed_ids)
        with pytest.raises(VerificationError, match="EdgeType vector"):
            await sync_read_ai(source, dataset_name=dataset_name)
        assert json.loads(journal.read_text())["last_success"] == checkpoint
        assert json.loads(journal.read_text())["attempt"] is not None
        assert await vector.retrieve("EdgeType_relationship_name", failed_ids)
    finally:
        provider.client.close()
