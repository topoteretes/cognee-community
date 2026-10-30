"""Unit tests for embed_data empty input guard.

These tests verify that embed_data() handles empty lists and blank
strings gracefully without calling the embedding API. Uses a mock
embedding engine to avoid real API calls.
"""

import asyncio
from unittest.mock import AsyncMock

from cognee_community_hybrid_adapter_falkor.falkor_adapter import FalkorDBAdapter


def _make_adapter_with_mock_engine(mock_embed_fn):
    """Create a FalkorDBAdapter with a mocked embedding engine."""
    adapter = object.__new__(FalkorDBAdapter)
    adapter.embedding_engine = AsyncMock()
    adapter.embedding_engine.embed_text = mock_embed_fn
    return adapter


def test_empty_list_returns_empty():
    mock_fn = AsyncMock(return_value=[])
    adapter = _make_adapter_with_mock_engine(mock_fn)

    result = asyncio.run(adapter.embed_data([]))

    assert result == []
    mock_fn.assert_not_called()


def test_all_blank_strings():
    mock_fn = AsyncMock(return_value=[])
    adapter = _make_adapter_with_mock_engine(mock_fn)

    result = asyncio.run(adapter.embed_data(["", "  ", "\n"]))

    assert result == [[], [], []]
    mock_fn.assert_not_called()


def test_normal_input_passes_through():
    expected = [[0.1, 0.2], [0.3, 0.4]]
    mock_fn = AsyncMock(return_value=expected)
    adapter = _make_adapter_with_mock_engine(mock_fn)

    result = asyncio.run(adapter.embed_data(["hello", "world"]))

    assert result == expected
    mock_fn.assert_called_once_with(["hello", "world"])


def test_mixed_blank_and_valid():
    """Blank strings are skipped but output positions are preserved."""
    mock_fn = AsyncMock(return_value=[[0.1, 0.2], [0.3, 0.4]])
    adapter = _make_adapter_with_mock_engine(mock_fn)

    result = asyncio.run(adapter.embed_data(["hello", "", "world", "  "]))

    assert len(result) == 4
    assert result[0] == [0.1, 0.2]  # "hello"
    assert result[1] == []  # "" (blank)
    assert result[2] == [0.3, 0.4]  # "world"
    assert result[3] == []  # "  " (blank)
    mock_fn.assert_called_once_with(["hello", "world"])


def test_single_valid_input():
    mock_fn = AsyncMock(return_value=[[0.5, 0.6]])
    adapter = _make_adapter_with_mock_engine(mock_fn)

    result = asyncio.run(adapter.embed_data(["test"]))

    assert result == [[0.5, 0.6]]
