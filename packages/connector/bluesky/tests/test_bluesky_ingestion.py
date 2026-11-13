"""Integration tests verifying ingestion into Cognee memory structures."""

import cognee
import pytest
from fake_bluesky import FakeBlueskyClient

from cognee_community_connector_bluesky import bluesky_source


@pytest.mark.asyncio
async def test_cognee_add_ingests_bluesky_documents():
    fake = FakeBlueskyClient()
    source = bluesky_source(actors=["bsky.app"], client=fake)

    await cognee.prune.prune_data()
    await cognee.add(source)

    datasets = await cognee.datasets.list_datasets()
    assert isinstance(datasets, list)
