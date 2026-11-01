"""The Rollbar source through the real cognee.add() pipeline, with the API faked.

remember() runs this same add() step before cognify(), which needs an LLM, so these
tests stop at the stored Data records.
"""

import json

import cognee
import pytest
from cognee.modules.data.methods import get_authorized_existing_datasets
from cognee.modules.data.methods.get_dataset_data import get_dataset_data
from cognee.modules.users.methods import get_default_user
from fake_rollbar import PROJECT, TOKEN, FakeRollbar, http_error

from cognee_community_connector_rollbar import rollbar_source
from cognee_community_connector_rollbar.rollbar import item_id

DATASET = "rollbar_integration_test"


def _project() -> tuple[FakeRollbar, dict, dict, dict]:
    fake = FakeRollbar()
    coupon = fake.add_item("ValueError: invalid coupon code")
    invoice = fake.add_item("KeyError: 'plan_tier'", environment="staging")
    margin = fake.add_item("ZeroDivisionError: division by zero")
    return fake, coupon, invoice, margin


async def _sync(fake: FakeRollbar):
    source = rollbar_source(PROJECT, access_token=TOKEN, http_client=fake.client())
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
        and data.system_metadata.get("source") == "rollbar"
    }


async def _document_ids() -> dict[str, object]:
    return {external_id: data.id for external_id, data in (await _documents()).items()}


@pytest.mark.asyncio
async def test_a_first_sync_stores_tagged_documents_and_a_quiet_one_changes_nothing(
    clean_environment,
):
    fake, coupon, invoice, margin = _project()
    await _sync(fake)
    first = await _documents()

    assert set(first) == {item_id(PROJECT, item) for item in (coupon, invoice, margin)}
    assert {doc.system_metadata["table_name"] for doc in first.values()} == {f"rollbar_{PROJECT}"}
    # cognee stores the node sets as json text
    assert sorted(json.loads(first[item_id(PROJECT, invoice)].node_set)) == [
        f"rollbar:{PROJECT}",
        f"rollbar:{PROJECT}:staging",
    ]

    fake.occur(coupon)
    quiet = await _sync(fake)
    assert quiet.cognee_sync_stats["skipped"] == 3
    assert await _document_ids() == {k: d.id for k, d in first.items()}


@pytest.mark.asyncio
async def test_a_resolved_item_replaces_only_its_document(clean_environment):
    fake, coupon, _, _ = _project()
    await _sync(fake)
    before = await _document_ids()

    fake.resolve(coupon, version="2.4.1")
    await _sync(fake)
    after = await _document_ids()

    changed = item_id(PROJECT, coupon)
    assert set(after) == set(before)
    assert after[changed] != before[changed]
    assert {k: v for k, v in after.items() if k != changed} == {
        k: v for k, v in before.items() if k != changed
    }


@pytest.mark.asyncio
async def test_an_item_whose_occurrences_were_deleted_is_forgotten(clean_environment):
    fake, coupon, invoice, margin = _project()
    await _sync(fake)

    fake.delete_item_occurrences(invoice)
    await _sync(fake)

    assert set(await _documents()) == {item_id(PROJECT, coupon), item_id(PROJECT, margin)}


@pytest.mark.asyncio
async def test_a_rate_limited_sync_resumes_without_losing_documents(clean_environment):
    fake, coupon, invoice, _ = _project()
    original = fake.handle
    reads = []

    def limited(request):
        if request.url.path.startswith("/api/1/instance/"):
            reads.append(request)
            if len(reads) == 3:
                return http_error(429)
        return original(request)

    fake.handle = limited
    cut = await _sync(fake)
    assert cut.cognee_sync_stats["failed_rate_limit"] == 1
    assert set(await _documents()) == {item_id(PROJECT, coupon), item_id(PROJECT, invoice)}

    fake.handle = original
    done = await _sync(fake)
    assert done.cognee_sync_stats["failed"] == 0
    assert len(await _documents()) == 3


@pytest.mark.asyncio
async def test_losing_access_mid_run_keeps_every_document(clean_environment):
    fake, coupon, invoice, _ = _project()
    await _sync(fake)
    fake.delete_item_occurrences(invoice)
    fake.resolve(coupon)
    original = fake.handle
    reads = []

    def revoked(request):
        if request.url.path.startswith("/api/1/instance/"):
            reads.append(request)
            if len(reads) == 2:
                return http_error(403, "token disabled")
        return original(request)

    fake.handle = revoked
    cut = await _sync(fake)

    assert cut.cognee_sync_stats["failed_access"] == 1
    assert len(await _documents()) == 3
