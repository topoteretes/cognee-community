"""The Checkly source through the real cognee.add() pipeline, with API faked."""

import cognee
import pytest
from cognee.modules.data.methods import get_authorized_existing_datasets
from cognee.modules.data.methods.get_dataset_data import get_dataset_data
from cognee.modules.users.methods import get_default_user
from fake_checkly import FakeCheckly

from cognee_community_connector_checkly import checkly_source

DATASET = "checkly_integration_test"
TABLE = "checkly_table"


def _workspace() -> FakeCheckly:
    fake = FakeCheckly()
    fake.add_check_group("g1", "CheckGroup", tags=["critical"])
    fake.add_check("c1", "APICheck", groupId="g1")
    fake.add_alert_channel("a1", "SlackAlert")
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
        if isinstance(data.system_metadata, dict)
        and data.system_metadata.get("source") == "checkly"
    }


@pytest.mark.asyncio
async def test_cognee_add_ingests_checkly_documents(clean_environment):
    fake = _workspace()
    source = checkly_source(service=fake, resource_name=TABLE)
    await _add(source)

    docs = await _documents()
    assert set(docs) == {"check_group:g1", "check:c1", "alert_channel:a1"}
    assert {doc.system_metadata["table_name"] for doc in docs.values()} == {TABLE}
    assert {doc.system_metadata["source"] for doc in docs.values()} == {"checkly"}

    quiet = checkly_source(service=fake, resource_name=TABLE)
    await _add(quiet)
    assert quiet.cognee_sync_stats["skipped"] == 3
