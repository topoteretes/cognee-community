"""Tests for examples/example.py script execution."""

from unittest.mock import AsyncMock, patch

import pytest

from examples.example import main


@pytest.mark.asyncio
async def test_example_exits_cleanly_when_no_api_key(monkeypatch):
    monkeypatch.delenv("KNOCK_API_KEY", raising=False)
    with pytest.raises(SystemExit) as exc_info:
        await main()
    assert exc_info.value.code == 0


@pytest.mark.asyncio
async def test_example_runs_end_to_end_with_mock(monkeypatch):
    monkeypatch.setenv("KNOCK_API_KEY", "mock_key_xyz")

    with (
        patch("examples.example.cognee.add", new_callable=AsyncMock) as mock_add,
        patch("examples.example.cognee.cognify", new_callable=AsyncMock) as mock_cognify,
        patch("examples.example.cognee.recall", new_callable=AsyncMock) as mock_recall,
    ):
        mock_recall.return_value = ["Knock Channel: Transactional SendGrid (email)"]
        await main()

        mock_add.assert_awaited_once()
        mock_cognify.assert_awaited_once()
        mock_recall.assert_awaited_once()
