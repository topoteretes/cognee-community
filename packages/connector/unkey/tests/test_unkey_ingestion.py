"""The Unkey source through the real cognee.add() pipeline, with API faked."""

import cognee
import pytest
from cognee.modules.data.methods import get_authorized_existing_datasets
from cognee.modules.data.methods.get_dataset_data import get_dataset_data
from cognee.modules.users.methods import get_default_user
from fake_unkey import FakeUnkey

from cognee_community_connector_unkey import unkey_source

DATASET = "unkey_integration_test"
TABLE = "unkey_table"


def _workspace() -> FakeUnkey:
    fake = FakeUnkey()
    fake.add_api("api_1", "UnkeyAPI")
    fake.add_key("api_1", "key_1", "UnkeyKey")
    fake.add_identity("id_1", externalId="UnkeyIdentity")
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
        if isinstance(data.system_metadata, dict) and data.system_metadata.get("source") == "unkey"
    }


@pytest.mark.asyncio
async def test_cognee_add_ingests_unkey_documents(clean_environment):
    fake = _workspace()
    source = unkey_source(service=fake, resource_name=TABLE)
    await _add(source)

    docs = await _documents()
    assert set(docs) == {"api:api_1", "key:api_1:key_1", "identity:id_1"}
    assert {doc.system_metadata["table_name"] for doc in docs.values()} == {TABLE}
    assert {doc.system_metadata["source"] for doc in docs.values()} == {"unkey"}

    quiet = unkey_source(service=fake, resource_name=TABLE)
    await _add(quiet)
    assert quiet.cognee_sync_stats["skipped"] == 3
