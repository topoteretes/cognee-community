"""The Trigger.dev source through the real cognee.add() pipeline, with the API faked.

remember() runs this same add() step before cognify(), which needs an LLM, so these
tests stop at the stored Data records. The fake answers at the HTTP layer, so the
real client and its URLs are part of every run.
"""

import cognee
import pytest
from cognee.modules.data.methods import get_authorized_existing_datasets
from cognee.modules.data.methods.get_dataset_data import get_dataset_data
from cognee.modules.users.methods import get_default_user
from fake_triggerdev import KEY, FakeTriggerDev

from cognee_community_connector_triggerdev import triggerdev_source

DATASET = "triggerdev_integration_test"
TABLE = "triggerdev_test"


def _environment(monkeypatch) -> FakeTriggerDev:
    fake = FakeTriggerDev()
    fake.add_schedule("sched_1", "nightly-backup")
    fake.add_schedule("sched_2", "sync-stripe")
    fake.add_error("error_1", "send-invoice")
    fake.add_run("run_1", "send-invoice")
    fake.add_deployment("deployment_1", "20261028.1")
    fake.install(monkeypatch)
    return fake


async def _sync(resource_name: str = TABLE, **options):
    source = triggerdev_source(api_key=KEY, resource_name=resource_name, **options)
    await cognee.add(
        source,
        dataset_name=DATASET,
        primary_key="id",
        write_disposition="merge",
        max_rows_per_table=0,
    )
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
        if isinstance(data.system_metadata, dict)
        and data.system_metadata.get("source") == "triggerdev"
    }


@pytest.mark.asyncio
async def test_a_first_sync_stores_tagged_documents_and_a_quiet_one_changes_nothing(
    clean_environment, monkeypatch
):
    _environment(monkeypatch)
    await _sync()
    first = await _documents()

    assert set(first) == {
        "schedule:sched_1",
        "schedule:sched_2",
        "error:error_1",
        "run:run_1",
        "deployment:deployment_1",
    }
    assert {doc.system_metadata["table_name"] for doc in first.values()} == {TABLE}

    quiet = await _sync()
    assert quiet.cognee_sync_stats["skipped"] == 5
    assert {k: d.id for k, d in (await _documents()).items()} == {k: d.id for k, d in first.items()}


@pytest.mark.asyncio
async def test_a_deleted_schedule_is_forgotten_and_history_stays(clean_environment, monkeypatch):
    fake = _environment(monkeypatch)
    await _sync()

    del fake.schedules["sched_2"]
    fake.runs.clear()  # the run aged out of the lookback window
    await _sync()

    assert set(await _documents()) == {
        "schedule:sched_1",
        "error:error_1",
        "run:run_1",
        "deployment:deployment_1",
    }


@pytest.mark.asyncio
async def test_a_failed_listing_forgets_nothing(clean_environment, monkeypatch):
    fake = _environment(monkeypatch)
    await _sync()
    before = set(await _documents())

    fake.fail["/api/v1/schedules"] = 500
    with pytest.raises(Exception, match="HTTP 500"):
        await _sync()

    assert set(await _documents()) == before


@pytest.mark.asyncio
async def test_an_error_group_that_gets_resolved_replaces_only_its_document(
    clean_environment, monkeypatch
):
    fake = _environment(monkeypatch)
    await _sync()
    before = {k: d.id for k, d in (await _documents()).items()}

    fake.errors["error_1"]["status"] = "resolved"
    await _sync()
    after = {k: d.id for k, d in (await _documents()).items()}

    assert after["error:error_1"] != before["error:error_1"]
    assert {k: v for k, v in after.items() if k != "error:error_1"} == {
        k: v for k, v in before.items() if k != "error:error_1"
    }


@pytest.mark.asyncio
async def test_a_rate_limited_first_sync_resumes_without_losing_documents(
    clean_environment, monkeypatch
):
    fake = _environment(monkeypatch)
    fake.fail["/api/v1/runs"] = 429
    cut = await _sync()
    assert cut.cognee_sync_stats["failed_rate_limit"] == 1
    assert set(await _documents()) == {"schedule:sched_1", "schedule:sched_2", "error:error_1"}

    del fake.fail["/api/v1/runs"]
    done = await _sync()
    assert "failed_rate_limit" not in done.cognee_sync_stats
    assert "run:run_1" in await _documents()


@pytest.mark.asyncio
async def test_two_environments_in_one_dataset_stay_apart(clean_environment, monkeypatch):
    fake = _environment(monkeypatch)
    await _sync(resource_name="triggerdev_prod")
    await _sync(resource_name="triggerdev_staging", include=("schedules",))

    del fake.schedules["sched_1"]
    await _sync(resource_name="triggerdev_staging", include=("schedules",))

    tables = {
        doc.system_metadata["table_name"]
        for key, doc in (await _documents()).items()
        if key == "schedule:sched_1"
    }
    # staging forgot its copy; prod still has its own until prod syncs again
    assert tables == {"triggerdev_prod"}
