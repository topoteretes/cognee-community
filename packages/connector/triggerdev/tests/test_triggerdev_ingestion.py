"""The Trigger.dev source through the real cognee.add() pipeline, with the API faked."""

import cognee
import pytest
from cognee.modules.data.methods import get_authorized_existing_datasets
from cognee.modules.data.methods.get_dataset_data import get_dataset_data
from cognee.modules.users.methods import get_default_user
from fake_triggerdev import FakeTriggerDev
from cognee_community_connector_triggerdev import triggerdev_source

DATASET = "triggerdev_integration_test"
TABLE = "triggerdev_test"


def _workspace() -> FakeTriggerDev:
    fake = FakeTriggerDev()
    fake.add_task("task-1")
    fake.add_schedule("sched-1", "task-1")
    fake.add_run("run-1", "task-1", status="COMPLETED")
    return fake


async def _add(source) -> None:
    await cognee.add(
        source,
        dataset_name=DATASET,
        primary_key="id",
        write_disposition="merge",
        max_rows_per_table=0,
    )


async def _sync(fake: FakeTriggerDev, resource_name: str = TABLE):
    source = triggerdev_source(service=fake, resource_name=resource_name)
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
        if isinstance(data.system_metadata, dict) and data.system_metadata.get("source") == "triggerdev"
    }


@pytest.mark.asyncio
async def test_first_sync_stores_documents_and_incremental_sync_skips(clean_environment):
    fake = _workspace()
    await _sync(fake)
    docs = await _documents()

    assert set(docs) == {"task:task_task-1", "schedule:sched-1", "run:run-1"}
    assert {doc.system_metadata["table_name"] for doc in docs.values()} == {TABLE}

    quiet = await _sync(fake)
    assert quiet.cognee_sync_stats["skipped"] == 3


@pytest.mark.asyncio
async def test_schedule_deleted_is_forgotten_from_memory(clean_environment):
    fake = _workspace()
    await _sync(fake)
    docs_before = await _documents()
    assert "schedule:sched-1" in docs_before

    del fake.schedules["sched-1"]
    await _sync(fake)
    docs_after = await _documents()

    assert "schedule:sched-1" not in docs_after
    assert "task:task_task-1" in docs_after
    assert "run:run-1" in docs_after
