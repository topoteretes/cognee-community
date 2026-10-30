"""The Copper source through the real cognee.add() pipeline, with the API faked.

remember() runs this same add() step before cognify(), which needs an LLM, so these
tests stop at the stored Data records.
"""

import cognee
import pytest
from cognee.modules.data.methods import get_authorized_existing_datasets
from cognee.modules.data.methods.get_dataset_data import get_dataset_data
from cognee.modules.users.methods import get_default_user
from fake_copper import FakeCopper

from cognee_community_connector_copper import copper as copper_module
from cognee_community_connector_copper import copper_source
from cognee_community_connector_copper.copper import (
    BASE_URL,
    CopperNotFoundError,
    CopperRateLimitedError,
)

DATASET = "copper_integration_test"
TABLE = "copper_test"
KEY = "SECRET-copper-key-123"
EMAIL = "owner@acme.example"


def _account(account_id: int = 1) -> FakeCopper:
    fake = FakeCopper(account_id=account_id)
    fake.add_company(1, "Acme Corp")
    fake.add_person(2, "Ada Lovelace", company_id=1, company_name="Acme Corp")
    fake.add_opportunity(3, "Acme renewal", company_id=1, company_name="Acme Corp")
    fake.add_activity(5, "Ada wants SSO before signing.", ("opportunity", 3))
    return fake


ALL = {"company:1", "person:2", "opportunity:3", "activity:5"}


async def _add(source) -> None:
    await cognee.add(
        source,
        dataset_name=DATASET,
        primary_key="id",
        write_disposition="merge",
        max_rows_per_table=0,
    )


async def _sync(fake: FakeCopper, resource_name: str = TABLE):
    source = copper_source(service=fake, resource_name=resource_name)
    await _add(source)
    return source


async def _documents() -> dict[tuple[str, str], object]:
    user = await get_default_user()
    datasets = await get_authorized_existing_datasets(
        user=user, permission_type="write", datasets=[DATASET]
    )
    if not datasets:
        return {}
    return {
        (data.system_metadata["table_name"], data.system_metadata["external_id"]): data
        for data in await get_dataset_data(datasets[0].id)
        if isinstance(data.system_metadata, dict) and data.system_metadata.get("source") == "copper"
    }


def _ids(documents: dict) -> set[str]:
    return {external_id for _, external_id in documents}


@pytest.mark.asyncio
async def test_a_first_sync_stores_tagged_documents_and_a_quiet_one_changes_nothing(
    clean_environment,
):
    fake = _account()
    await _sync(fake)
    first = await _documents()

    assert _ids(first) == ALL
    assert {table for table, _ in first} == {TABLE}

    quiet = await _sync(fake)
    assert quiet.cognee_sync_stats["skipped"] == 4
    assert {k: d.id for k, d in (await _documents()).items()} == {k: d.id for k, d in first.items()}


@pytest.mark.asyncio
async def test_an_edited_activity_replaces_only_that_document(clean_environment):
    fake = _account()
    await _sync(fake)
    before = {k: d.id for k, d in (await _documents()).items()}

    fake.activities[5]["details"] = "Ada signed the contract."
    await _sync(fake)
    after = {k: d.id for k, d in (await _documents()).items()}

    activity = (TABLE, "activity:5")
    assert set(after) == set(before)
    assert after[activity] != before[activity]
    assert {k: v for k, v in after.items() if k != activity} == {
        k: v for k, v in before.items() if k != activity
    }


@pytest.mark.asyncio
async def test_an_opportunity_deleted_in_copper_is_forgotten(clean_environment):
    fake = _account()
    await _sync(fake)

    del fake.opportunities[3]
    del fake.activities[5]  # copper deletes the activities of a deleted record with it
    await _sync(fake)

    assert _ids(await _documents()) == ALL - {"opportunity:3", "activity:5"}


@pytest.mark.asyncio
async def test_a_rate_limited_first_sync_resumes_without_losing_documents(clean_environment):
    fake = _account()
    fake.fail_from = ("/opportunities/search", 1)
    fake.fail_with = CopperRateLimitedError("Copper rate limit exceeded (HTTP 429)")
    cut = await _sync(fake)
    assert cut.cognee_sync_stats["failed_rate_limit"] == 1
    assert _ids(await _documents()) == {"company:1", "person:2"}

    fake.fail_after = fake.fail_from = None
    done = await _sync(fake)
    assert done.cognee_sync_stats["failed"] == 0
    assert _ids(await _documents()) == ALL


class _HTTPResponse:
    def __init__(self, status: int, payload):
        self.status_code = status
        self._payload = payload
        self.headers = {}

    def json(self):
        return self._payload


def _serve(fake: FakeCopper, seen: list[tuple[str, str]]):
    """Route requests.request into the fake, the way the real HTTP API would answer."""

    def request(method, url, *, json=None, headers=None, timeout=None, allow_redirects=True):
        seen.append((headers["X-PW-AccessToken"], headers["X-PW-UserEmail"]))
        try:
            payload = fake.request(method, url.removeprefix(BASE_URL), body=json)
        except CopperNotFoundError:
            return _HTTPResponse(404, {"message": "Resource not found"})
        return _HTTPResponse(200, payload)

    return request


@pytest.mark.asyncio
async def test_the_real_client_syncs_and_the_key_never_reaches_stored_documents(
    clean_environment, monkeypatch
):
    fake, seen = _account(), []
    monkeypatch.setattr(copper_module.requests, "request", _serve(fake, seen))
    source = copper_source(api_key=KEY, user_email=EMAIL, resource_name=TABLE)
    assert KEY not in repr(source)

    await _add(source)
    del fake.activities[5]
    await _add(copper_source(api_key=KEY, user_email=EMAIL, resource_name=TABLE))

    documents = await _documents()
    assert _ids(documents) == ALL - {"activity:5"}
    assert seen and set(seen) == {(KEY, EMAIL)}
    for doc in documents.values():
        assert KEY not in repr(doc)
        with open(doc.raw_data_location.removeprefix("file://"), encoding="utf-8") as stored:
            assert KEY not in stored.read()


@pytest.mark.asyncio
async def test_two_copper_accounts_in_one_dataset_stay_apart(clean_environment):
    account_a, account_b = _account(), FakeCopper(account_id=2)
    account_b.add_opportunity(30, "Globex pilot")
    account_b.add_opportunity(31, "Initech upsell")

    await _sync(account_a, "copper_a")
    await _sync(account_b, "copper_b")
    both = {("copper_a", i) for i in ALL} | {
        ("copper_b", "opportunity:30"),
        ("copper_b", "opportunity:31"),
    }
    assert set(await _documents()) == both

    del account_b.opportunities[31]
    await _sync(account_b, "copper_b")
    await _sync(account_a, "copper_a")

    assert set(await _documents()) == both - {("copper_b", "opportunity:31")}
