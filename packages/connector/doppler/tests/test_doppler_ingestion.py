"""The Doppler source through the real cognee.add() pipeline, with the API faked."""

import cognee
import pytest
from cognee.modules.data.methods import get_authorized_existing_datasets
from cognee.modules.data.methods.get_dataset_data import get_dataset_data
from cognee.modules.users.methods import get_default_user
from fake_doppler import FakeDoppler
from cognee_community_connector_doppler import doppler_source

DATASET = "doppler_integration_test"
TABLE = "doppler_test"


def _workspace() -> FakeDoppler:
    fake = FakeDoppler()
    fake.add_project("api-service")
    fake.add_environment("api-service", "prd", "Production")
    fake.add_config("api-service", "prd_main", "prd", ["DATABASE_URL"])
    return fake


async def _add(source) -> None:
    await cognee.add(
        source,
        dataset_name=DATASET,
        primary_key="id",
        write_disposition="merge",
        max_rows_per_table=0,
    )


async def _sync(fake: FakeDoppler, resource_name: str = TABLE):
    source = doppler_source(service=fake, resource_name=resource_name)
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
        if isinstance(data.system_metadata, dict) and data.system_metadata.get("source") == "doppler"
    }


@pytest.mark.asyncio
async def test_doppler_ingestion_and_incremental_sync(clean_environment):
    fake = _workspace()
    await _sync(fake)
    docs = await _documents()

    assert set(docs) == {"project:api-service", "environment:api-service:prd", "config:api-service:prd_main"}
    assert {doc.system_metadata["table_name"] for doc in docs.values()} == {TABLE}

    quiet = await _sync(fake)
    assert quiet.cognee_sync_stats["skipped"] == 3


@pytest.mark.asyncio
async def test_doppler_environment_deleted_is_forgotten(clean_environment):
    fake = _workspace()
    await _sync(fake)
    docs_before = await _documents()
    assert "environment:api-service:prd" in docs_before

    fake.environments["api-service"] = []
    await _sync(fake)
    docs_after = await _documents()

    assert "environment:api-service:prd" not in docs_after
    assert "project:api-service" in docs_after
