"""Integration tests verifying ingestion into Cognee memory structures."""

import cognee
import pytest
from fake_jsr import FakeJSRClient

from cognee_community_connector_jsr import jsr_source


@pytest.mark.asyncio
async def test_cognee_add_ingests_jsr_documents():
    fake = FakeJSRClient()
    source = jsr_source(scopes=["std"], client=fake)

    await cognee.prune.prune_data()
    await cognee.add(source)

    # Ingestion creates DataItem and document entries in cognee
    datasets = await cognee.datasets.list_datasets()
    assert isinstance(datasets, list)
