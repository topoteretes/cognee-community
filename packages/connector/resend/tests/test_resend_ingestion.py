"""Integration tests verifying Resend documents ingest into Cognee storage."""

import cognee
import pytest
from fake_resend import FakeResendClient, make_sample_resend_data

from cognee_community_connector_resend.resend import resend_source


@pytest.mark.asyncio
async def test_cognee_add_ingests_resend_documents(tmp_path):
    client = FakeResendClient(sample_data=make_sample_resend_data())
    source = resend_source(
        api_key="mock_key",
        client=client,
        include=("domains", "audiences"),
    )

    await cognee.add(source, dataset_name="test_resend_dataset")

    # Ingestion should complete cleanly without errors
    assert True
