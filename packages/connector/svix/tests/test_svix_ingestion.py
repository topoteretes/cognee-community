"""The Svix source through the real cognee.add() pipeline, with API faked."""

import cognee
import pytest
from cognee.modules.data.methods import get_authorized_existing_datasets
from cognee.modules.data.methods.get_dataset_data import get_dataset_data
from cognee.modules.users.methods import get_default_user
from fake_svix import FakeSvix

from cognee_community_connector_svix import svix_source

DATASET = "svix_integration_test"
TABLE = "svix_table"


def _workspace() -> FakeSvix:
    fake = FakeSvix()
    fake.add_app("app_1", "SvixApp")
    fake.add_endpoint(
        "app_1", "ep_1", "https://api.example.com/hook", description="WebhookEndpoint"
    )
    fake.add_event_type("EventType")
    return fake


async def _add(source) -> None:
    await cognee.add(
        source,
        dataset_name=DATASET,
        primary_key="id",
        write_disposition="merge",
        max_rows_per_table=0,
    )


async def _documents() -> dict[str, object]:
    user = await get_default_user()
    datasets = await get_authorized_existing_datasets(
        user=user, permission_type="write", datasets=[DATASET]
    )
    if not datasets:
        return {}
    return {
        data.system_metadata["external_id"]: data
        for data in await get_dataset_data(datasets[0].id)
        if isinstance(data.system_metadata, dict) and data.system_metadata.get("source") == "svix"
    }


@pytest.mark.asyncio
async def test_cognee_add_ingests_svix_documents(clean_environment):
    fake = _workspace()
    source = svix_source(service=fake, resource_name=TABLE)
    await _add(source)

    docs = await _documents()
    assert set(docs) == {"app:app_1", "endpoint:app_1:ep_1", "event_type:EventType"}
    assert {doc.system_metadata["table_name"] for doc in docs.values()} == {TABLE}
    assert {doc.system_metadata["source"] for doc in docs.values()} == {"svix"}

    quiet = svix_source(service=fake, resource_name=TABLE)
    await _add(quiet)
    assert quiet.cognee_sync_stats["skipped"] == 3
