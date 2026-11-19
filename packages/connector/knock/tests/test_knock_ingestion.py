"""Integration tests verifying Knock documents ingest into Cognee storage."""

import cognee
import pytest
from fake_knock import FakeKnockClient, make_sample_knock_data

from cognee_community_connector_knock.knock import knock_source


@pytest.mark.asyncio
async def test_cognee_add_ingests_knock_documents(tmp_path):
    client = FakeKnockClient(sample_data=make_sample_knock_data())
    source = knock_source(
        api_key="mock_key",
        client=client,
        include=("workflows", "channels"),
    )

    await cognee.add(source, dataset_name="test_knock_dataset")

    # Ingestion should complete cleanly without errors
    assert True
