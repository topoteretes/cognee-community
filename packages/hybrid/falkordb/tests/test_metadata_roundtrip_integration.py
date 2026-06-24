"""Integration test: a node's dict `metadata` round-trips as a dict through a real FalkorDB.

This exercises the path the cognee 1.2.0 `namespace_entity_type_node_ids` migration
uses: write a node whose `metadata` is a dict, then read it back via `get_graph_data`.
Before the fix, `metadata` came back as a JSON string and the migration's `add_nodes`
raised `TypeError: string indices must be integers, not 'str'`.

Requires a FalkorDB at localhost:6379 (skipped otherwise):
    docker run --rm -d -p 6379:6379 falkordb/falkordb:latest
"""

from unittest.mock import MagicMock

import pytest
from cognee_community_hybrid_adapter_falkor.falkor_adapter import FalkorDBAdapter


def _falkordb_available() -> bool:
    try:
        from falkordb import FalkorDB

        FalkorDB(host="localhost", port=6379).list_graphs()
        return True
    except Exception:
        return False


pytestmark = pytest.mark.skipif(
    not _falkordb_available(),
    reason="needs FalkorDB at localhost:6379 (docker run -p 6379:6379 falkordb/falkordb:latest)",
)


@pytest.mark.asyncio
async def test_metadata_roundtrips_as_dict_through_falkordb():
    adapter = FalkorDBAdapter(
        graph_database_url="localhost",
        graph_database_port=6379,
        embedding_engine=MagicMock(),  # avoids get_embedding_engine(); no embedding happens here
        database_name="metadata_roundtrip_itest",
    )

    await adapter.add_node(
        "n1",
        {
            "id": "n1",
            "type": "Entity",
            "name": "Example",
            "metadata": {"index_fields": ["name"], "type": "Entity"},
        },
    )

    nodes, _ = await adapter.get_graph_data()

    node = next(n for n in nodes if n[0] == "n1")
    metadata = node[1]["metadata"]
    assert isinstance(metadata, dict), (
        f"metadata came back as {type(metadata).__name__}, expected dict"
    )
    assert metadata["index_fields"] == ["name"]
