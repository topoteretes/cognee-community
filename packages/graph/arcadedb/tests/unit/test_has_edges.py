"""Offline unit tests for ArcadeDBAdapter.has_edges (cognee 1.6.x contract).

Issue #419: has_edges returned a list of booleans aligned with the query rows,
but cognee core's find_existing_edge_identities unpacks each returned item into
three values, so has_edges must return the subset of the input triples that
exist, as (source_id, target_id, relationship_name) tuples.

No database required — query() is stubbed.
"""

import pytest

from cognee_community_graph_adapter_arcadedb.arcadedb_adapter import ArcadeDBAdapter


class StubbedQueryAdapter(ArcadeDBAdapter):
    def __init__(self, rows):
        # Skip super().__init__: no driver/connection needed for these tests.
        self._rows = rows
        self.seen_queries = []

    async def query(self, query, params=None):
        self.seen_queries.append((query, params))
        return self._rows


@pytest.mark.asyncio
async def test_has_edges_returns_existing_triples_not_booleans():
    adapter = StubbedQueryAdapter(
        [
            {"from_node": "a", "to_node": "b", "relationship_name": "KNOWS", "edge_exists": True},
            {"from_node": "b", "to_node": "c", "relationship_name": "LIKES", "edge_exists": True},
        ]
    )

    result = await adapter.has_edges(
        [("a", "b", "KNOWS"), ("b", "c", "LIKES"), ("c", "d", "HATES")]
    )

    assert result == [("a", "b", "KNOWS"), ("b", "c", "LIKES")]
    assert all(isinstance(item, tuple) for item in result)


@pytest.mark.asyncio
async def test_has_edges_filters_out_missing_edges():
    adapter = StubbedQueryAdapter(
        [
            {"from_node": "a", "to_node": "b", "relationship_name": "KNOWS", "edge_exists": True},
            {"from_node": "b", "to_node": "c", "relationship_name": "LIKES", "edge_exists": False},
        ]
    )

    result = await adapter.has_edges([("a", "b", "KNOWS"), ("b", "c", "LIKES")])

    assert result == [("a", "b", "KNOWS")]


@pytest.mark.asyncio
async def test_has_edges_results_are_unpackable_by_core():
    # Mirrors cognee/modules/graph/utils/retrieve_existing_edges.py:
    #   for source_id, target_id, relationship_name in await adapter.has_edges(edges)
    adapter = StubbedQueryAdapter(
        [{"from_node": "a", "to_node": "b", "relationship_name": "KNOWS", "edge_exists": True}]
    )

    unpacked = [f"{s}->{t}:{r}" for s, t, r in await adapter.has_edges([("a", "b", "KNOWS")])]

    assert unpacked == ["a->b:KNOWS"]


@pytest.mark.asyncio
async def test_has_edges_empty_input_returns_empty():
    adapter = StubbedQueryAdapter([])

    assert await adapter.has_edges([]) == []
