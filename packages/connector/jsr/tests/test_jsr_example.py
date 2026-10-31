"""Tests verifying example workflow execution."""

from unittest.mock import AsyncMock, patch

import pytest
from fake_jsr import FakeJSRClient

from cognee_community_connector_jsr import jsr_source


@pytest.mark.asyncio
async def test_example_runs_end_to_end_with_mock():
    fake = FakeJSRClient()
    source = jsr_source(scopes=["std"], client=fake)

    with (
        patch("cognee.prune.prune_data", new_callable=AsyncMock) as mock_prune_data,
        patch("cognee.prune.prune_system", new_callable=AsyncMock) as mock_prune_sys,
        patch("cognee.add", new_callable=AsyncMock) as mock_add,
        patch("cognee.cognify", new_callable=AsyncMock) as mock_cognify,
        patch(
            "cognee.search", new_callable=AsyncMock, return_value=["Sample result"]
        ) as mock_search,
    ):
        await mock_prune_data()
        await mock_prune_sys(metadata=True)
        await mock_add(source)
        await mock_cognify()
        res = await mock_search("path utilities")

        assert res == ["Sample result"]
        assert mock_add.called
        assert mock_cognify.called
