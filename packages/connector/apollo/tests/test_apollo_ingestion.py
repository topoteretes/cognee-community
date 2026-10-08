"""The Apollo source through the real cognee.add() pipeline, with the API faked.

remember() runs this same add() step before cognify(), which needs an LLM, so these
tests stop at the stored Data records.
"""

import cognee
import pytest
from cognee.modules.data.methods import get_authorized_existing_datasets
from cognee.modules.data.methods.get_dataset_data import get_dataset_data
from cognee.modules.users.methods import get_default_user
from fake_apollo import FakeApollo

from cognee_community_connector_apollo import apollo_source
from cognee_community_connector_apollo.apollo import ApolloRateLimitedError

DATASET = "apollo_integration_test"
TABLE = "apollo_test"
KEY = "SECRET-apollo-key-123"


def _workspace() -> FakeApollo:
    fake = FakeApollo()
    fake.add_sequence("s1", "Q4 Outreach")
    fake.add_account("a1", "Acme")
    fake.add_contact("c1", "Ada Lovelace", account_id="a1")
    fake.add_contact("c2", "Ben Turing", account_id="a1")
    return fake


async def _sync(fake: FakeApollo):
    source = apollo_source(service=fake, resource_name=TABLE)
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
        if isinstance(data.system_metadata, dict) and data.system_metadata.get("source") == "apollo"
    }


@pytest.mark.asyncio
async def test_a_first_sync_stores_tagged_documents_and_a_quiet_one_changes_nothing(
    clean_environment,
):
    fake = _workspace()
    await _sync(fake)
    first = await _documents()

    assert set(first) == {"sequence:s1", "account:a1", "contact:c1", "contact:c2"}
    assert {doc.system_metadata["table_name"] for doc in first.values()} == {TABLE}

    quiet = await _sync(fake)
    assert quiet.cognee_sync_stats["skipped"] == 4
    assert {k: d.id for k, d in (await _documents()).items()} == {k: d.id for k, d in first.items()}


@pytest.mark.asyncio
async def test_an_enrollment_replaces_only_that_contacts_document(clean_environment):
    fake = _workspace()
    await _sync(fake)
    before = {k: d.id for k, d in (await _documents()).items()}

    fake.enroll("c1", "s1")
    await _sync(fake)
    after = {k: d.id for k, d in (await _documents()).items()}

    assert set(after) == set(before)
    assert after["contact:c1"] != before["contact:c1"]
    assert {k: v for k, v in after.items() if k != "contact:c1"} == {
        k: v for k, v in before.items() if k != "contact:c1"
    }


@pytest.mark.asyncio
async def test_a_contact_deleted_in_apollo_is_forgotten(clean_environment):
    fake = _workspace()
    await _sync(fake)

    del fake.contacts["c2"]
    await _sync(fake)

    assert set(await _documents()) == {"sequence:s1", "account:a1", "contact:c1"}


@pytest.mark.asyncio
async def test_a_rate_limited_first_sync_resumes_without_losing_documents(clean_environment):
    fake = _workspace()
    fake.fail_after = 6  # lookups and accounts pass, contacts search fails
    fake.fail_with = ApolloRateLimitedError("Apollo rate limit exceeded (HTTP 429)")
    cut = await _sync(fake)
    assert cut.cognee_sync_stats["failed_rate_limit"] == 1
    assert set(await _documents()) == {"sequence:s1", "account:a1"}

    fake.fail_after = None
    done = await _sync(fake)
    assert done.cognee_sync_stats["failed"] == 0
    assert set(await _documents()) == {"sequence:s1", "account:a1", "contact:c1", "contact:c2"}


@pytest.mark.asyncio
async def test_the_api_key_never_reaches_stored_documents(clean_environment):
    fake = _workspace()
    source = apollo_source(api_key=KEY, resource_name=TABLE)
    assert KEY not in repr(source)

    await _sync(fake)
    for doc in (await _documents()).values():
        assert KEY not in repr(doc.system_metadata)
