"""Unit test ensuring the example script runs cleanly with mock inputs."""

from unittest.mock import AsyncMock, patch

import pytest
from fake_traefik import FakeTraefik


@pytest.mark.asyncio
async def test_example_runs_with_mocked_cognee():
    fake = FakeTraefik()
    fake.add_http_router(
        "test-router",
        rule="Host(`example.com`)",
        service="test-svc",
    )

    with (
        patch("cognee.remember", new_callable=AsyncMock) as mock_remember,
        patch("cognee.search", new_callable=AsyncMock) as mock_search,
    ):
        mock_remember.return_value = {"status": "success"}
        mock_search.return_value = "Router test-router has no middlewares attached."

        from examples.example import sync

        await sync("http://traefik.mock:8080")
        assert mock_remember.called
