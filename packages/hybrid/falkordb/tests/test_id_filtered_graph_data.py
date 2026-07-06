"""Unit tests for get_id_filtered_graph_data().

Verifies the id-scoped subgraph projection used by cognee's
GraphCompletionRetriever: node/edge tuple shapes, edge direction taken from
properties(r), and seed<->seed de-dup. No FalkorDB connection is required —
uses a mock driver, same style as test_query_return_type.py.
"""

import asyncio
from unittest.mock import MagicMock

from cognee_community_hybrid_adapter_falkor.falkor_adapter import FalkorDBAdapter


def _make_adapter_with_mock_query(result_set):
    """Create a FalkorDBAdapter whose query() returns a fixed result_set."""
    adapter = object.__new__(FalkorDBAdapter)
    adapter.graph_name = "test_graph"

    mock_result = MagicMock()
    mock_result.result_set = result_set
    mock_result.header = []

    mock_graph = MagicMock()
    mock_graph.query.return_value = mock_result

    mock_driver = MagicMock()
    mock_driver.select_graph.return_value = mock_graph

    adapter.driver = mock_driver
    return adapter, mock_graph


def test_empty_target_ids_short_circuits():
    adapter, mock_graph = _make_adapter_with_mock_query([])
    nodes, edges = asyncio.run(adapter.get_id_filtered_graph_data([]))
    assert nodes == [] and edges == []
    mock_graph.query.assert_not_called()  # never touches the DB


def test_projects_nodes_and_directed_edges():
    rows = [
        [
            {"id": "s1", "name": "seed"},
            {"id": "n1", "name": "neighbor"},
            "RELATES_TO",
            {"source_node_id": "s1", "target_node_id": "n1"},
        ],
    ]
    adapter, _ = _make_adapter_with_mock_query(rows)
    nodes, edges = asyncio.run(adapter.get_id_filtered_graph_data(["s1"]))
    assert dict(nodes) == {
        "s1": {"id": "s1", "name": "seed"},
        "n1": {"id": "n1", "name": "neighbor"},
    }
    assert edges == [
        ("s1", "n1", "RELATES_TO", {"source_node_id": "s1", "target_node_id": "n1"})
    ]


def test_direction_from_rel_props_not_match_order():
    # Undirected match can bind `a` to the target end; direction must come from
    # properties(r), so the edge stays s1 -> n1 even though a == n1 here.
    rows = [
        [
            {"id": "n1"},
            {"id": "s1"},
            "RELATES_TO",
            {"source_node_id": "s1", "target_node_id": "n1"},
        ],
    ]
    adapter, _ = _make_adapter_with_mock_query(rows)
    _, edges = asyncio.run(adapter.get_id_filtered_graph_data(["s1"]))
    assert edges == [
        ("s1", "n1", "RELATES_TO", {"source_node_id": "s1", "target_node_id": "n1"})
    ]


def test_seed_to_seed_edge_deduped():
    # Undirected match returns a seed<->seed edge twice (once per anchor).
    e = {"source_node_id": "s1", "target_node_id": "s2"}
    rows = [
        [{"id": "s1"}, {"id": "s2"}, "REL", e],
        [{"id": "s2"}, {"id": "s1"}, "REL", e],
    ]
    adapter, _ = _make_adapter_with_mock_query(rows)
    nodes, edges = asyncio.run(adapter.get_id_filtered_graph_data(["s1", "s2"]))
    assert {n[0] for n in nodes} == {"s1", "s2"}
    assert len(edges) == 1
    assert edges[0][:3] == ("s1", "s2", "REL")


def test_falls_back_to_endpoints_when_rel_props_lack_ids():
    rows = [[{"id": "s1"}, {"id": "n1"}, "REL", {}]]
    adapter, _ = _make_adapter_with_mock_query(rows)
    _, edges = asyncio.run(adapter.get_id_filtered_graph_data(["s1"]))
    assert edges == [("s1", "n1", "REL", {})]
