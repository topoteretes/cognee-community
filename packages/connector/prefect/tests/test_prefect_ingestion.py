"""The Prefect source through the real cognee.add() pipeline, with API faked."""

import cognee
import pytest
from cognee.modules.data.methods import get_authorized_existing_datasets
from cognee.modules.data.methods.get_dataset_data import get_dataset_data
from cognee.modules.users.methods import get_default_user
from fake_prefect import FakePrefect

from cognee_community_connector_prefect import prefect_source

DATASET = "prefect_integration_test"
TABLE = "prefect_table"


def _workspace() -> FakePrefect:
    fake = FakePrefect()
    fake.add_flow("f1", "ECommerceFlow", tags=["critical"])
    fake.add_deployment("d1", "ETLDeployment", "f1")
    fake.add_flow_run("r1", "Run 1", "f1", deployment_id="d1")
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
        and data.system_metadata.get("source") == "prefect"
    }


@pytest.mark.asyncio
async def test_cognee_add_ingests_prefect_documents(clean_environment):
    fake = _workspace()
    source = prefect_source(service=fake, resource_name=TABLE)
    await _add(source)

    docs = await _documents()
    assert set(docs) == {"flow:f1", "deployment:d1", "flow_run:r1"}
    assert {doc.system_metadata["table_name"] for doc in docs.values()} == {TABLE}
    assert {doc.system_metadata["source"] for doc in docs.values()} == {"prefect"}

    quiet = prefect_source(service=fake, resource_name=TABLE)
    await _add(quiet)
    assert quiet.cognee_sync_stats["skipped"] == 3
