"""The Amplitude source through the real cognee.add() pipeline, with the API faked.

remember() runs this same add() step before cognify(), which needs an LLM, so these
tests stop at the stored Data records.
"""

import cognee
import pytest
from cognee.modules.data.methods import get_authorized_existing_datasets
from cognee.modules.data.methods.get_dataset_data import get_dataset_data
from cognee.modules.users.methods import get_default_user
from fake_amplitude import COHORTS, EVENTS, FakeAmplitude

from cognee_community_connector_amplitude import amplitude_source
from cognee_community_connector_amplitude.amplitude import (
    AmplitudeAccessError,
    AmplitudeRateLimitedError,
)

DATASET = "amplitude_integration_test"
TABLE = "amplitude_test"
KEY = "SECRET-amplitude-key-123"
SECRET = "SECRET-amplitude-secret-456"
ALL_IDS = {"event:Checkout Completed", "user_property:gp:plan_tier", "cohort:c1", "annotation:1"}


def _project() -> FakeAmplitude:
    fake = FakeAmplitude()
    fake.add_event("Checkout Completed", description="A customer paid for an order.")
    fake.add_event_property("Checkout Completed", "cart_value", type="number")
    fake.add_user_property("gp:plan_tier", description="Plan the account is on.")
    fake.add_cohort("c1", "Paying users")
    fake.add_annotation(1, "Checkout redesign shipped")
    return fake


async def _sync(fake: FakeAmplitude, chart_ids: list[str] | None = None):
    source = amplitude_source(service=fake, resource_name=TABLE, chart_ids=chart_ids)
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
        and data.system_metadata.get("source") == "amplitude"
    }


async def _document_ids() -> dict[str, object]:
    return {external_id: data.id for external_id, data in (await _documents()).items()}


@pytest.mark.asyncio
async def test_a_first_sync_stores_tagged_documents_and_a_quiet_one_changes_nothing(
    clean_environment,
):
    fake = _project()
    await _sync(fake)
    first = await _documents()

    assert set(first) == ALL_IDS
    assert {doc.system_metadata["table_name"] for doc in first.values()} == {TABLE}

    fake.recompute()
    quiet = await _sync(fake)
    assert quiet.cognee_sync_stats["skipped"] == 4
    assert await _document_ids() == {k: d.id for k, d in first.items()}


@pytest.mark.asyncio
async def test_an_edited_property_replaces_only_its_events_document(clean_environment):
    fake = _project()
    await _sync(fake)
    before = await _document_ids()

    fake.event_properties["Checkout Completed"]["cart_value"]["description"] = "Order total."
    await _sync(fake)
    after = await _document_ids()

    changed = "event:Checkout Completed"
    assert set(after) == set(before)
    assert after[changed] != before[changed]
    assert {k: v for k, v in after.items() if k != changed} == {
        k: v for k, v in before.items() if k != changed
    }


@pytest.mark.asyncio
async def test_records_deleted_or_archived_in_amplitude_are_forgotten(clean_environment):
    fake = _project()
    await _sync(fake)

    fake.cohorts["c1"]["archived"] = True
    del fake.annotations[1]
    await _sync(fake)

    assert set(await _documents()) == {"event:Checkout Completed", "user_property:gp:plan_tier"}


@pytest.mark.asyncio
async def test_a_named_chart_is_stored_and_forgotten_once_it_is_deleted(clean_environment):
    fake = _project()
    fake.add_chart("ch1", "Checkouts per day", "Uniques", "Checkout Completed")
    await _sync(fake, chart_ids=["ch1"])
    assert set(await _documents()) == ALL_IDS | {"chart:ch1"}

    del fake.charts["ch1"]
    await _sync(fake, chart_ids=["ch1"])

    assert set(await _documents()) == ALL_IDS


@pytest.mark.asyncio
async def test_a_rate_limited_first_sync_resumes_without_losing_documents(clean_environment):
    fake = _project()
    fake.fail_paths[COHORTS] = AmplitudeRateLimitedError("Amplitude rate limit exceeded (HTTP 429)")
    cut = await _sync(fake)
    assert cut.cognee_sync_stats["failed_rate_limit"] == 1
    assert set(await _documents()) == {"event:Checkout Completed", "user_property:gp:plan_tier"}

    del fake.fail_paths[COHORTS]
    done = await _sync(fake)
    assert done.cognee_sync_stats["failed"] == 0
    assert set(await _documents()) == ALL_IDS


@pytest.mark.asyncio
async def test_losing_access_to_a_kind_keeps_its_documents(clean_environment):
    fake = _project()
    await _sync(fake)

    fake.fail_paths[EVENTS] = AmplitudeAccessError("The Amplitude plan cannot call it (HTTP 403)")
    skipped = await _sync(fake)

    assert skipped.cognee_sync_stats["no_access"] == 1
    assert set(await _documents()) == ALL_IDS


@pytest.mark.asyncio
async def test_the_credentials_never_reach_stored_documents(clean_environment):
    fake = _project()
    source = amplitude_source(api_key=KEY, secret_key=SECRET, resource_name=TABLE)
    assert KEY not in repr(source)
    assert SECRET not in repr(source)

    await _sync(fake)
    for doc in (await _documents()).values():
        assert KEY not in repr(doc.system_metadata)
        assert SECRET not in repr(doc.system_metadata)
