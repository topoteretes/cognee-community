"""The Pipedrive source through the real cognee.add() pipeline, with the API faked.

remember() runs this same add() step before cognify(), which needs an LLM, so these
tests stop at the stored Data records.
"""

import cognee
import pytest
from cognee.modules.data.methods import get_authorized_existing_datasets
from cognee.modules.data.methods.get_dataset_data import get_dataset_data
from cognee.modules.users.methods import get_default_user
from fake_pipedrive import FakePipedrive

from cognee_community_connector_pipedrive import pipedrive as pipedrive_module
from cognee_community_connector_pipedrive import pipedrive_source
from cognee_community_connector_pipedrive.pipedrive import (
    BASE_URL,
    PipedriveNotFoundError,
    PipedriveRateLimitedError,
)

DATASET = "pipedrive_integration_test"
TABLE = "pipedrive_test"
TOKEN = "SECRET-pipedrive-token-123"


def _company(company_id: int = 1) -> FakePipedrive:
    fake = FakePipedrive(company_id=company_id)
    fake.add_organization(1, "Acme Corp")
    fake.add_person(2, "Ada Lovelace", org_id=1)
    fake.add_deal(3, "Acme renewal", org_id=1, person_id=2)
    fake.add_note(5, "<p>Ada wants SSO before signing.</p>", deal_id=3)
    return fake


ALL = {"organization:1", "person:2", "deal:3", "note:5"}


async def _add(source) -> None:
    await cognee.add(
        source,
        dataset_name=DATASET,
        primary_key="id",
        write_disposition="merge",
        max_rows_per_table=0,
    )


async def _sync(fake: FakePipedrive, resource_name: str = TABLE):
    source = pipedrive_source(service=fake, resource_name=resource_name)
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
        (data.system_metadata["table_name"], data.system_metadata["external_id"]): data
        for data in await get_dataset_data(datasets[0].id)
        if isinstance(data.system_metadata, dict)
        and data.system_metadata.get("source") == "pipedrive"
    }


def _ids(documents: dict) -> set[str]:
    return {external_id for _, external_id in documents}


@pytest.mark.asyncio
async def test_a_first_sync_stores_tagged_documents_and_a_quiet_one_changes_nothing(
    clean_environment,
):
    fake = _company()
    await _sync(fake)
    first = await _documents()

    assert _ids(first) == ALL
    assert {table for table, _ in first} == {TABLE}

    quiet = await _sync(fake)
    assert quiet.cognee_sync_stats["skipped"] == 4
    assert {k: d.id for k, d in (await _documents()).items()} == {k: d.id for k, d in first.items()}


@pytest.mark.asyncio
async def test_an_edited_note_replaces_only_that_notes_document(clean_environment):
    fake = _company()
    await _sync(fake)
    before = {k: d.id for k, d in (await _documents()).items()}

    fake.notes[5]["content"] = "<p>Ada signed the contract.</p>"
    await _sync(fake)
    after = {k: d.id for k, d in (await _documents()).items()}

    note = (TABLE, "note:5")
    assert set(after) == set(before)
    assert after[note] != before[note]
    assert {k: v for k, v in after.items() if k != note} == {
        k: v for k, v in before.items() if k != note
    }


@pytest.mark.asyncio
async def test_a_note_deleted_in_pipedrive_is_forgotten(clean_environment):
    fake = _company()
    await _sync(fake)

    del fake.notes[5]
    await _sync(fake)

    assert _ids(await _documents()) == ALL - {"note:5"}


@pytest.mark.asyncio
async def test_a_rate_limited_first_sync_resumes_without_losing_documents(clean_environment):
    fake = _company()
    fake.fail_from = ("/api/v2/deals", 1)
    fake.fail_with = PipedriveRateLimitedError("Pipedrive rate limit exceeded (HTTP 429)")
    cut = await _sync(fake)
    assert cut.cognee_sync_stats["failed_rate_limit"] == 1
    assert _ids(await _documents()) == {"organization:1", "person:2"}

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


def _serve(fake: FakePipedrive, tokens: list[str]):
    """Route requests.get into the fake, the way the real HTTP API would answer."""

    def get(url, *, params=None, headers=None, timeout=None, allow_redirects=True):
        tokens.append(headers["x-api-token"])
        try:
            payload = fake.request(url.removeprefix(BASE_URL), params=params)
        except PipedriveNotFoundError:
            return _HTTPResponse(404, {"success": False, "error": "not found"})
        return _HTTPResponse(200, payload)

    return get


@pytest.mark.asyncio
async def test_the_real_client_syncs_and_the_token_never_reaches_stored_documents(
    clean_environment, monkeypatch
):
    fake, tokens = _company(), []
    monkeypatch.setattr(pipedrive_module.requests, "get", _serve(fake, tokens))
    source = pipedrive_source(api_token=TOKEN, resource_name=TABLE)
    assert TOKEN not in repr(source)

    await _add(source)
    del fake.notes[5]
    await _add(pipedrive_source(api_token=TOKEN, resource_name=TABLE))

    documents = await _documents()
    assert _ids(documents) == ALL - {"note:5"}
    assert tokens and set(tokens) == {TOKEN}
    for doc in documents.values():
        assert TOKEN not in repr(doc)
        with open(doc.raw_data_location.removeprefix("file://"), encoding="utf-8") as stored:
            assert TOKEN not in stored.read()


@pytest.mark.asyncio
async def test_two_pipedrive_companies_in_one_dataset_stay_apart(clean_environment):
    company_a, company_b = _company(), FakePipedrive(company_id=2)
    company_b.add_deal(30, "Globex pilot")
    company_b.add_deal(31, "Initech upsell")

    await _sync(company_a, "pipedrive_a")
    await _sync(company_b, "pipedrive_b")
    both = {("pipedrive_a", i) for i in ALL} | {
        ("pipedrive_b", "deal:30"),
        ("pipedrive_b", "deal:31"),
    }
    assert set(await _documents()) == both

    del company_b.deals[31]
    await _sync(company_b, "pipedrive_b")
    await _sync(company_a, "pipedrive_a")

    assert set(await _documents()) == both - {("pipedrive_b", "deal:31")}
