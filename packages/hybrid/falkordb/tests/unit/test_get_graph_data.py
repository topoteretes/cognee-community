"""Regression tests for FalkorDBAdapter.get_graph_data() edge endpoints.

get_graph_data() must read edge endpoints from the columns its Cypher query
returns (the node ``id`` properties). It used to read them from
``properties(r)``, but neither ``add_edge()`` nor ``add_edges()`` ever writes
``source_node_id``/``target_node_id`` onto the relationship, so any graph with
an adapter-written edge raised ``KeyError: 'source_node_id'``.

No FalkorDB connection is required.
"""

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from cognee_community_hybrid_adapter_falkor.falkor_adapter import FalkorDBAdapter


class _FakeGraph:
    """Minimal stand-in for a FalkorDB graph connection.

    Emulates how FalkorDB fills the requested columns: ``ID(n)`` yields the
    engine's internal integer id, while ``n.id`` yields the adapter-written
    ``id`` property (a string).
    """

    def __init__(self, nodes, edges):
        # nodes: [(internal_id, properties)], edges: [(source, target, type, properties)]
        self._nodes = nodes
        self._edges = edges

    def query(self, cypher, params=None):
        if cypher.strip().startswith("MATCH (n) RETURN ID(n)"):
            rows = [(internal_id, ["Node"], props) for internal_id, props in self._nodes]
        elif "MATCH (n)-[r]->(m)" in cypher:
            by_internal_id = {internal_id: props for internal_id, props in self._nodes}
            if "ID(n) AS source" in cypher:
                rows = list(self._edges)
            else:
                rows = [
                    (
                        by_internal_id[source]["id"],
                        by_internal_id[target]["id"],
                        rel_type,
                        props,
                    )
                    for source, target, rel_type, props in self._edges
                ]
        else:
            raise AssertionError(f"unexpected query: {cypher}")
        return SimpleNamespace(result_set=rows)


def _make_adapter(graph):
    with patch.object(FalkorDBAdapter, "__init__", lambda self, **kw: None):
        adapter = FalkorDBAdapter()
    adapter.driver = MagicMock()
    adapter.driver.select_graph.return_value = graph
    adapter.graph_name = "test_graph"
    return adapter


@pytest.mark.asyncio
async def test_get_graph_data_returns_endpoints_for_graph_with_edge():
    """A graph containing an edge must not raise; endpoints are the node ids."""
    edge_properties = {"relationship_name": "REL", "weight": 3}
    graph = _FakeGraph(
        nodes=[
            (0, {"id": "node-a", "name": "a"}),
            (1, {"id": "node-b", "name": "b"}),
        ],
        edges=[(0, 1, "REL", edge_properties)],
    )

    nodes, edges = await _make_adapter(graph).get_graph_data()

    assert [node_id for node_id, _ in nodes] == ["node-a", "node-b"]
    assert edges == [("node-a", "node-b", "REL", edge_properties)]


@pytest.mark.asyncio
async def test_get_graph_data_prefers_actual_endpoints_over_edge_properties():
    """Endpoints come from the graph structure, not from edge properties.

    Edges migrated from the Postgres adapter carry embedded
    ``source_node_id``/``target_node_id`` properties that can disagree with the
    real endpoints; the returned ids must be the actual ones.
    """
    graph = _FakeGraph(
        nodes=[
            (0, {"id": "node-a"}),
            (1, {"id": "node-b"}),
        ],
        edges=[(0, 1, "REL", {"source_node_id": "stale", "target_node_id": "stale"})],
    )

    _, edges = await _make_adapter(graph).get_graph_data()

    assert edges == [
        ("node-a", "node-b", "REL", {"source_node_id": "stale", "target_node_id": "stale"})
    ]
