"""Unit tests for query() return type compliance.

Verifies that query() returns a list (GraphDBInterface contract)
while maintaining backward compatibility via the result_set attribute.
No FalkorDB connection is required — uses a mock driver.
"""

import asyncio
from unittest.mock import MagicMock

from cognee_community_hybrid_adapter_falkor.falkor_adapter import FalkorDBAdapter


def _make_adapter_with_mock_query(result_set, header=None):
    """Create a FalkorDBAdapter with a mocked FalkorDB driver."""
    adapter = object.__new__(FalkorDBAdapter)
    adapter.graph_name = "test_graph"

    mock_result = MagicMock()
    mock_result.result_set = result_set
    mock_result.header = header or []

    mock_graph = MagicMock()
    mock_graph.query.return_value = mock_result

    mock_driver = MagicMock()
    mock_driver.select_graph.return_value = mock_graph

    adapter.driver = mock_driver
    return adapter


def test_query_returns_list():
    """query() must return a list, not a QueryResult."""
    adapter = _make_adapter_with_mock_query([[1, "Alice"]])
    result = asyncio.run(adapter.query("MATCH (n) RETURN n.id, n.name"))
    assert isinstance(result, list)


def test_query_result_set_backward_compat():
    """result.result_set should point to the same list for backward compat."""
    rows = [[1, "Alice"], [2, "Bob"]]
    adapter = _make_adapter_with_mock_query(rows)
    result = asyncio.run(adapter.query("MATCH (n) RETURN n.id, n.name"))

    assert result.result_set is result
    assert len(result.result_set) == 2
    assert result.result_set[0] == [1, "Alice"]


def test_query_empty_result():
    """Empty query result should return an empty list."""
    adapter = _make_adapter_with_mock_query([])
    result = asyncio.run(adapter.query("MATCH (n) RETURN n"))
    assert result == []
    assert result.result_set == []


def test_query_none_result_set():
    """None result_set should return an empty list."""
    adapter = _make_adapter_with_mock_query(None)
    result = asyncio.run(adapter.query("MATCH (n) RETURN n"))
    assert result == []


def test_query_result_is_iterable():
    """Result must be iterable (required by CYPHER search serialization)."""
    adapter = _make_adapter_with_mock_query([[42]])
    result = asyncio.run(adapter.query("RETURN 42"))
    assert list(result) == [[42]]


def test_query_result_supports_bool():
    """Non-empty result should be truthy, empty should be falsy."""
    adapter_nonempty = _make_adapter_with_mock_query([[1]])
    assert asyncio.run(adapter_nonempty.query("RETURN 1"))

    adapter_empty = _make_adapter_with_mock_query([])
    assert not asyncio.run(adapter_empty.query("RETURN 1"))
