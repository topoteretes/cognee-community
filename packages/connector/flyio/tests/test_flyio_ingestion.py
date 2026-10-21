"""The Fly.io source through the real cognee.add() pipeline, with the API faked."""

import cognee
import pytest
from cognee.modules.data.methods import get_authorized_existing_datasets
from cognee.modules.data.methods.get_dataset_data import get_dataset_data
from cognee.modules.users.methods import get_default_user
from fake_flyio import FakeFlyio
from cognee_community_connector_flyio import flyio_source

DATASET = "flyio_integration_test"
TABLE = "flyio_test"


def _workspace() -> FakeFlyio:
    fake = FakeFlyio()
    fake.add_app("EdgeProxyApp")
    fake.add_machine("EdgeProxyApp", "MachineIad1", "edge-iad-1", region="iad")
    return fake


async def _add(source) -> None:
    await cognee.add(
        source,
        dataset_name=DATASET,
        primary_key="id",
        write_disposition="merge",
        max_rows_per_table=0,
    )


async def _sync(fake: FakeFlyio, resource_name: str = TABLE):
    source = flyio_source(service=fake, resource_name=resource_name, include=("apps", "machines"))
    await _add(source)
    return source


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
        if isinstance(data.system_metadata, dict) and data.system_metadata.get("source") == "flyio"
    }


@pytest.mark.asyncio
async def test_flyio_ingestion_and_incremental_sync(clean_environment):
    fake = _workspace()
    await _sync(fake)
    docs = await _documents()

    assert set(docs) == {"app:EdgeProxyApp", "machine:EdgeProxyApp:MachineIad1"}
    assert {doc.system_metadata["table_name"] for doc in docs.values()} == {TABLE}

    quiet = await _sync(fake)
    assert quiet.cognee_sync_stats["skipped"] == 2


@pytest.mark.asyncio
async def test_flyio_machine_deleted_is_forgotten(clean_environment):
    fake = _workspace()
    await _sync(fake)
    docs_before = await _documents()
    assert "machine:EdgeProxyApp:MachineIad1" in docs_before

    fake.machines["EdgeProxyApp"] = []
    await _sync(fake)
    docs_after = await _documents()

    assert "machine:EdgeProxyApp:MachineIad1" not in docs_after
    assert "app:EdgeProxyApp" in docs_after
