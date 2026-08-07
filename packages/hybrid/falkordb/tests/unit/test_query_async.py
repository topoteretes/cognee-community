"""Tests for FalkorDBAdapter.query() async compatibility.

These tests verify that query() conforms to Cognee's GraphDBInterface
contract (async def query()) without requiring a running FalkorDB server.
"""

import inspect
from unittest.mock import MagicMock, patch

import pytest
from cognee_community_hybrid_adapter_falkor.falkor_adapter import FalkorDBAdapter


def test_query_is_coroutine_function():
    """query() must be async def to satisfy GraphDBInterface contract."""
    assert inspect.iscoroutinefunction(FalkorDBAdapter.query), (
        "FalkorDBAdapter.query() must be a coroutine function (async def) "
        "to conform to Cognee's GraphDBInterface"
    )


@pytest.mark.asyncio
async def test_query_is_awaitable():
    """query() result must be awaitable for `await engine.query(...)` usage."""
    with patch.object(FalkorDBAdapter, "__init__", lambda self, **kw: None):
        adapter = FalkorDBAdapter()
        mock_graph = MagicMock()
        mock_graph.query.return_value = MagicMock(result_set=[])
        adapter.driver = MagicMock()
        adapter.driver.select_graph.return_value = mock_graph
        adapter.graph_name = "test_graph"

        # This must not raise TypeError
        result = await adapter.query("RETURN 1")
        assert result is not None
