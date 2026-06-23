"""Unit tests for Memgraph graph-metrics computation.

These tests exercise the pure component helper and ``get_graph_metrics()`` with a
stubbed ``get_graph_data()``, so they run without a live Memgraph instance.

Regression coverage for the bug where ``get_graph_metrics()`` filtered on
``:Node``/``:EDGE`` labels the adapter never writes and indexed dict-shaped query
results positionally, making every metric collapse to the all-zeros fallback.
"""

from unittest.mock import AsyncMock

import pytest

from cognee_community_graph_adapter_memgraph.memgraph_adapter import (
    MemgraphAdapter,
    weakly_connected_component_sizes,
)


def _make_adapter(nodes, edges):
    """Build a MemgraphAdapter without touching the DB, stubbing get_graph_data()."""
    adapter = MemgraphAdapter.__new__(MemgraphAdapter)
    adapter.get_graph_data = AsyncMock(return_value=(nodes, edges))
    return adapter


# --------------------------------------------------------------------------- #
# weakly_connected_component_sizes
# --------------------------------------------------------------------------- #


def test_component_sizes_two_components():
    sizes = weakly_connected_component_sizes([1, 2, 3, 4, 5], [(1, 2), (2, 3), (4, 5)])
    assert sizes == [3, 2]


def test_component_sizes_singletons():
    assert weakly_connected_component_sizes([1, 2, 3], []) == [1, 1, 1]


def test_component_sizes_treats_edges_as_undirected():
    assert weakly_connected_component_sizes([1, 2], [(2, 1)]) == [2]


def test_component_sizes_empty():
    assert weakly_connected_component_sizes([], []) == []


def test_component_sizes_ignores_unknown_endpoints():
    # An edge referencing a node id that is not in the node list must not crash.
    assert weakly_connected_component_sizes([1, 2], [(1, 99)]) == [1, 1]


def test_component_sizes_self_loop_stays_singleton():
    assert weakly_connected_component_sizes([1, 2], [(1, 1)]) == [1, 1]


# --------------------------------------------------------------------------- #
# get_graph_metrics
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_get_graph_metrics_connected_graph():
    nodes = [(1, {}), (2, {}), (3, {})]
    edges = [(1, 2, "REL", {}), (2, 3, "REL", {})]
    metrics = await _make_adapter(nodes, edges).get_graph_metrics()

    assert metrics["num_nodes"] == 3
    assert metrics["num_edges"] == 2
    assert metrics["num_connected_components"] == 1
    assert metrics["sizes_of_connected_components"] == [3]
    assert metrics["mean_degree"] == pytest.approx(4 / 3)
    assert metrics["edge_density"] == pytest.approx(2 / (3 * 2))
    # Optional metrics are off by default.
    assert metrics["num_selfloops"] == -1
    assert metrics["diameter"] == -1


@pytest.mark.asyncio
async def test_get_graph_metrics_disconnected_with_selfloop():
    # Components: {1, 2}, {3} (self-loop), {4} (isolated).
    nodes = [(1, {}), (2, {}), (3, {}), (4, {})]
    edges = [(1, 2, "REL", {}), (3, 3, "REL", {})]
    metrics = await _make_adapter(nodes, edges).get_graph_metrics(include_optional=True)

    assert metrics["num_nodes"] == 4
    assert metrics["num_edges"] == 2
    assert metrics["num_connected_components"] == 3
    assert metrics["sizes_of_connected_components"] == [2, 1, 1]
    assert metrics["num_selfloops"] == 1
    # Expensive all-pairs metrics remain unsupported.
    assert metrics["diameter"] == -1
    assert metrics["avg_shortest_path_length"] == -1
    assert metrics["avg_clustering"] == -1


@pytest.mark.asyncio
async def test_get_graph_metrics_empty_graph():
    metrics = await _make_adapter([], []).get_graph_metrics()

    assert metrics["num_nodes"] == 0
    assert metrics["num_edges"] == 0
    assert metrics["mean_degree"] == 0
    assert metrics["edge_density"] == 0
    assert metrics["num_connected_components"] == 0
    assert metrics["sizes_of_connected_components"] == []
