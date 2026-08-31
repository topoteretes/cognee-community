import asyncio

import pytest

from cognee_community_graph_adapter_arcadedb.arcadedb_adapter import ArcadeDBAdapter


class RecordingAdapter(ArcadeDBAdapter):
    def __init__(self):
        self.calls = []
        self.active_queries = 0
        self.max_active_queries = 0

    async def query(self, query, params=None):
        self.calls.append((query, params))
        self.active_queries += 1
        self.max_active_queries = max(
            self.max_active_queries, self.active_queries
        )

        await asyncio.sleep(0)

        self.active_queries -= 1

        if "MATCH (node)<-[relation]" in query:
            return []

        if "MATCH (node)-[relation]->(neighbour)" in query:
            return []

        return []


@pytest.mark.asyncio
async def test_get_connections_executes_queries_sequentially():
    adapter = RecordingAdapter()

    result = await adapter.get_connections("node-1")

    assert result == []
    assert len(adapter.calls) == 2
    assert adapter.max_active_queries == 1


class NeighborAdapter(RecordingAdapter):
    async def get_predecessors(self, node_id):
        self.active_queries += 1
        self.max_active_queries = max(
            self.max_active_queries, self.active_queries
        )

        await asyncio.sleep(0)

        self.active_queries -= 1
        return [{"id": "predecessor"}]

    async def get_successors(self, node_id):
        self.active_queries += 1
        self.max_active_queries = max(
            self.max_active_queries, self.active_queries
        )

        await asyncio.sleep(0)

        self.active_queries -= 1
        return [{"id": "successor"}]


@pytest.mark.asyncio
async def test_get_neighbors_executes_queries_sequentially():
    adapter = NeighborAdapter()

    result = await adapter.get_neighbors("node-1")

    assert result == [
        {"id": "predecessor"},
        {"id": "successor"},
    ]
    assert adapter.max_active_queries == 1
