"""Unit test verifying the example script runs cleanly with mock inputs."""

from unittest.mock import AsyncMock, patch

import pytest


@pytest.mark.asyncio
async def test_example_runs_with_mocked_cognee():
    sample_scan = {
        "SchemaVersion": 2,
        "ArtifactName": "test-app:1.0",
        "Results": [],
    }

    with (
        patch("cognee.remember", new_callable=AsyncMock) as mock_remember,
        patch("cognee.search", new_callable=AsyncMock) as mock_search,
    ):
        mock_remember.return_value = {"status": "success"}
        mock_search.return_value = "No critical CVEs found."

        from examples.example import sync

        await sync(sample_scan)
        assert mock_remember.called
