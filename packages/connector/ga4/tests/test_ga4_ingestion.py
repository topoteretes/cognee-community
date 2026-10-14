"""The GA4 source through the real cognee.add() pipeline, with the Admin API faked.

remember() runs this same add() step before cognify(), which needs an LLM, so these
tests stop at the stored Data records.
"""

import cognee
import pytest
from cognee.modules.data.methods import get_authorized_existing_datasets
from cognee.modules.data.methods.get_dataset_data import get_dataset_data
from cognee.modules.users.methods import get_default_user
from fake_ga4 import PROPERTY, FakeGA4

from cognee_community_connector_ga4 import ga4_source
from cognee_community_connector_ga4.ga4 import GA4AccessError, GA4RateLimitedError

DATASET = "ga4_integration_test"
BASE = f"properties/{PROPERTY}"
DIMENSION = f"{BASE}/customDimensions/11"
KEY_EVENT = f"{BASE}/keyEvents/31"
AUDIENCE = f"{BASE}/audiences/41"
ANNOTATION = f"{BASE}/reportingDataAnnotations/51"
ALL_IDS = {DIMENSION, KEY_EVENT, AUDIENCE, ANNOTATION}


def _property() -> FakeGA4:
    fake = FakeGA4()
    fake.add_custom_dimension("11", "plan_tier", "Plan tier", scope="USER")
    fake.add_key_event("31", "purchase")
    fake.add_audience("41", "Purchasers", "Users who bought something.")
    fake.add_annotation("51", "Checkout redesign shipped")
    return fake


async def _sync(fake: FakeGA4):
    source = ga4_source(PROPERTY, service=fake)
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
        if isinstance(data.system_metadata, dict) and data.system_metadata.get("source") == "ga4"
    }


async def _document_ids() -> dict[str, object]:
    return {external_id: data.id for external_id, data in (await _documents()).items()}


@pytest.mark.asyncio
async def test_a_first_sync_stores_tagged_documents_and_a_quiet_one_changes_nothing(
    clean_environment,
):
    fake = _property()
    await _sync(fake)
    first = await _documents()

    assert set(first) == ALL_IDS
    assert {doc.system_metadata["table_name"] for doc in first.values()} == {f"ga4_{PROPERTY}"}

    quiet = await _sync(fake)
    assert quiet.cognee_sync_stats["skipped"] == 4
    assert await _document_ids() == {k: d.id for k, d in first.items()}


@pytest.mark.asyncio
async def test_an_edited_audience_replaces_only_its_document(clean_environment):
    fake = _property()
    await _sync(fake)
    before = await _document_ids()

    fake.records["audiences"]["41"]["description"] = "Users who bought twice."
    await _sync(fake)
    after = await _document_ids()

    assert set(after) == set(before)
    assert after[AUDIENCE] != before[AUDIENCE]
    assert {k: v for k, v in after.items() if k != AUDIENCE} == {
        k: v for k, v in before.items() if k != AUDIENCE
    }


@pytest.mark.asyncio
async def test_definitions_archived_or_deleted_in_ga4_are_forgotten(clean_environment):
    fake = _property()
    await _sync(fake)

    del fake.records["custom_dimensions"]["11"]
    del fake.records["annotations"]["51"]
    await _sync(fake)

    assert set(await _documents()) == {KEY_EVENT, AUDIENCE}


@pytest.mark.asyncio
async def test_a_rate_limited_first_sync_resumes_without_losing_documents(clean_environment):
    fake = _property()
    fake.fail_kinds["audiences"] = GA4RateLimitedError("quota exhausted (HTTP 429)")
    cut = await _sync(fake)
    assert cut.cognee_sync_stats["failed_rate_limit"] == 1
    assert set(await _documents()) == {DIMENSION, KEY_EVENT}

    del fake.fail_kinds["audiences"]
    done = await _sync(fake)
    assert done.cognee_sync_stats["failed"] == 0
    assert set(await _documents()) == ALL_IDS


@pytest.mark.asyncio
async def test_losing_access_mid_run_keeps_every_document(clean_environment):
    fake = _property()
    await _sync(fake)
    del fake.records["annotations"]["51"]

    fake.fail_kinds["key_events"] = GA4AccessError("cannot read (HTTP 403)")
    fake.records["custom_dimensions"]["11"]["description"] = "edited, so the run makes progress"
    cut = await _sync(fake)

    assert cut.cognee_sync_stats["failed_access"] == 1
    assert set(await _documents()) == ALL_IDS
