"""The Axiom source through the real cognee.add() pipeline, with the API faked."""

import cognee
import pytest
from cognee.modules.data.methods import get_authorized_existing_datasets
from cognee.modules.data.methods.get_dataset_data import get_dataset_data
from cognee.modules.users.methods import get_default_user
from fake_axiom import FakeAxiom
from cognee_community_connector_axiom import axiom_source

DATASET = "axiom_integration_test"
TABLE = "axiom_test"


def _workspace() -> FakeAxiom:
    fake = FakeAxiom()
    fake.add_dataset("production-http")
    fake.add_monitor("HttpSpikeMonitor", "HTTP Error Spike", "['production-http'] | where status >= 500")
    return fake


async def _add(source) -> None:
    await cognee.add(
        source,
        dataset_name=DATASET,
        primary_key="id",
        write_disposition="merge",
        max_rows_per_table=0,
    )


async def _sync(fake: FakeAxiom, resource_name: str = TABLE):
    source = axiom_source(service=fake, resource_name=resource_name, include=("datasets", "monitors"))
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
        if isinstance(data.system_metadata, dict) and data.system_metadata.get("source") == "axiom"
    }


@pytest.mark.asyncio
async def test_axiom_ingestion_and_incremental_sync(clean_environment):
    fake = _workspace()
    await _sync(fake)
    docs = await _documents()

    assert set(docs) == {"dataset:production-http", "monitor:HttpSpikeMonitor"}
    assert {doc.system_metadata["table_name"] for doc in docs.values()} == {TABLE}

    quiet = await _sync(fake)
    assert quiet.cognee_sync_stats["skipped"] == 2


@pytest.mark.asyncio
async def test_axiom_monitor_deleted_is_forgotten(clean_environment):
    fake = _workspace()
    await _sync(fake)
    docs_before = await _documents()
    assert "monitor:HttpSpikeMonitor" in docs_before

    fake.monitors = []
    await _sync(fake)
    docs_after = await _documents()

    assert "monitor:HttpSpikeMonitor" not in docs_after
    assert "dataset:production-http" in docs_after
