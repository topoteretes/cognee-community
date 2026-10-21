"""The Inngest source through the real cognee.add() pipeline, with the API faked."""

import cognee
import pytest
from cognee.modules.data.methods import get_authorized_existing_datasets
from cognee.modules.data.methods.get_dataset_data import get_dataset_data
from cognee.modules.users.methods import get_default_user
from fake_inngest import FakeInngest
from cognee_community_connector_inngest import inngest_source

DATASET = "inngest_integration_test"
TABLE = "inngest_test"


def _workspace() -> FakeInngest:
    fake = FakeInngest()
    fake.add_function("SendWelcomeEmail", "Send Welcome Email")
    fake.add_event("user/signup.completed")
    return fake


async def _add(source) -> None:
    await cognee.add(
        source,
        dataset_name=DATASET,
        primary_key="id",
        write_disposition="merge",
        max_rows_per_table=0,
    )


async def _sync(fake: FakeInngest, resource_name: str = TABLE):
    source = inngest_source(service=fake, resource_name=resource_name, include=("functions", "events"))
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
        if isinstance(data.system_metadata, dict) and data.system_metadata.get("source") == "inngest"
    }


@pytest.mark.asyncio
async def test_inngest_ingestion_and_incremental_sync(clean_environment):
    fake = _workspace()
    await _sync(fake)
    docs = await _documents()

    assert set(docs) == {"function:SendWelcomeEmail", "event:evt_user/signup.completed_1"}
    assert {doc.system_metadata["table_name"] for doc in docs.values()} == {TABLE}

    quiet = await _sync(fake)
    assert quiet.cognee_sync_stats["skipped"] == 2


@pytest.mark.asyncio
async def test_inngest_function_deleted_is_forgotten(clean_environment):
    fake = _workspace()
    await _sync(fake)
    docs_before = await _documents()
    assert "function:SendWelcomeEmail" in docs_before

    fake.functions = []
    await _sync(fake)
    docs_after = await _documents()

    assert "function:SendWelcomeEmail" not in docs_after
    assert "event:evt_user/signup.completed_1" in docs_after
