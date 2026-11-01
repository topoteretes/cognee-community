"""The Fathom source through the real ``cognee.add()`` pipeline, with the API faked.

``remember()`` runs this same ``add()`` step before ``cognify()``, which needs an LLM, so
these tests stop at the stored Data records; ``test_fathom_forget.py`` goes on to the graph.
"""

import json

import cognee
import pytest
import responses
from cognee.modules.data.methods import get_authorized_existing_datasets
from cognee.modules.data.methods.get_dataset_data import get_dataset_data
from cognee.modules.users.methods import get_default_user
from fake_fathom import NOW, FakeFathom

from cognee_community_connector_fathom import FathomClient, fathom_source
from cognee_community_connector_fathom import fathom as fathom_module

DATASET = "fathom_integration_test"
TABLE = "fathom_test"
KEY = "SECRET-fathom-key-123"


@pytest.fixture(autouse=True)
def _frozen_clock(monkeypatch):
    monkeypatch.setattr(fathom_module, "_now", lambda: NOW)


@pytest.fixture
def api():
    fake = FakeFathom()
    with responses.RequestsMock() as rsps:
        fake.install(rsps)
        yield fake


def _workspace(api: FakeFathom) -> FakeFathom:
    api.add(101, days_ago=60, title="Acme pricing review")
    api.add(102, days_ago=45, title="Roadmap sync", calendar_invitees_domains_type="only_internal")
    return api


def _source(**kwargs):
    kwargs.setdefault("client", FathomClient(KEY, sleep=lambda _seconds: None))
    return fathom_source(resource_name=TABLE, **kwargs)


async def _add(source, **kwargs) -> None:
    kwargs.setdefault("write_disposition", "merge")
    await cognee.add(source, dataset_name=DATASET, primary_key="id", **kwargs)


async def _sync(**kwargs):
    source = _source(**kwargs)
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
        if isinstance(data.system_metadata, dict) and data.system_metadata.get("source") == "fathom"
    }


def _node_sets(document) -> set[str]:
    raw = document.node_set
    return set(json.loads(raw) if isinstance(raw, str) else raw)


async def _text(document) -> str:
    from cognee.infrastructure.files.utils.open_data_file import open_data_file

    async with open_data_file(document.raw_data_location, mode="r", encoding="utf-8") as handle:
        return handle.read()


@pytest.mark.asyncio
async def test_a_first_sync_stores_one_tagged_document_per_meeting(clean_environment, api):
    _workspace(api)

    source = await _sync()
    docs = await _documents()

    assert set(docs) == {"101", "102"}
    assert {doc.system_metadata["table_name"] for doc in docs.values()} == {TABLE}
    assert docs["101"].system_metadata["url"] == "https://fathom.video/calls/101"
    assert source.cognee_sync_stats["fetched"] == 2

    text = await _text(docs["101"])
    assert text.startswith("# Acme pricing review")
    assert "We agreed pricing for deal 101." in text
    assert "- [ ] Send the proposal 101 (Alice Johnson)" in text
    assert "Jane Smith (acme.com, external)" in text


@pytest.mark.asyncio
async def test_no_email_address_or_share_link_is_stored_anywhere(clean_environment, api):
    _workspace(api)
    await _sync(include_transcript=True, include_highlights=True)

    for doc in (await _documents()).values():
        stored = await _text(doc) + json.dumps(doc.system_metadata, default=str)
        stored += json.dumps(doc.external_metadata, default=str)
        assert "@" not in stored
        assert "secret101" not in stored and "secret102" not in stored


@pytest.mark.asyncio
async def test_a_quiet_sync_changes_no_document_and_makes_no_heavy_request(clean_environment, api):
    _workspace(api)
    await _sync()
    first = {k: d.id for k, d in (await _documents()).items()}
    api.requests.clear()

    quiet = await _sync()

    assert quiet.cognee_sync_stats["fetched"] == 0
    assert api.heavy_requests == []
    assert {k: d.id for k, d in (await _documents()).items()} == first


@pytest.mark.asyncio
async def test_a_refetched_but_unchanged_meeting_keeps_its_document_id(clean_environment, api):
    api.add(1, days_ago=2)  # inside the re-check window: fetched again on every run
    await _sync()
    before = {k: d.id for k, d in (await _documents()).items()}
    api.requests.clear()

    again = await _sync()

    assert again.cognee_sync_stats["fetched"] == 1
    assert api.heavy_requests
    assert {k: d.id for k, d in (await _documents()).items()} == before


@pytest.mark.asyncio
async def test_a_completed_action_item_replaces_only_that_meetings_document(clean_environment, api):
    _workspace(api)
    await _sync()
    before = {k: d.id for k, d in (await _documents()).items()}

    api.meetings[101]["action_items"][0]["completed"] = True
    await _sync()
    after = await _documents()

    assert set(after) == set(before)
    assert after["101"].id != before["101"]
    assert after["102"].id == before["102"]
    assert "- [x] Send the proposal 101" in await _text(after["101"])


@pytest.mark.asyncio
async def test_a_new_meeting_is_added_without_touching_the_others(clean_environment, api):
    _workspace(api)
    await _sync()
    before = {k: d.id for k, d in (await _documents()).items()}

    api.add(103, days_ago=0.2, title="Fresh call")
    await _sync()
    after = {k: d.id for k, d in (await _documents()).items()}

    assert set(after) == {"101", "102", "103"}
    assert {k: v for k, v in after.items() if k != "103"} == before


@pytest.mark.asyncio
async def test_a_meeting_deleted_in_fathom_is_forgotten(clean_environment, api):
    _workspace(api)
    await _sync()

    api.remove(102)
    source = await _sync()

    assert set(await _documents()) == {"101"}
    assert source.cognee_sync_stats["deleted"] == 1


@pytest.mark.asyncio
async def test_a_failed_listing_forgets_nothing(clean_environment, api):
    _workspace(api)
    await _sync()
    api.remove(102)
    api.script = [500] * 20

    with pytest.raises(Exception, match="Fathom"):
        await _sync()

    assert set(await _documents()) == {"101", "102"}

    api.script = []
    await _sync()
    assert set(await _documents()) == {"101"}


@pytest.mark.asyncio
async def test_a_rate_limited_sync_leaves_memory_alone_and_the_next_one_finishes(
    clean_environment, api
):
    _workspace(api)
    api.script = [429] * 20

    with pytest.raises(Exception, match="rate limited"):
        await _sync()
    assert await _documents() == {}

    api.script = []
    await _sync()
    assert set(await _documents()) == {"101", "102"}


@pytest.mark.asyncio
async def test_replace_is_refused_before_it_can_forget_unchanged_meetings(clean_environment, api):
    _workspace(api)
    await _sync()
    api.requests.clear()

    with pytest.raises(Exception, match="write_disposition='merge'") as raised:
        await _add(_source(), write_disposition="replace")

    assert "Fathom source" in str(raised.value)
    assert api.requests == []
    assert set(await _documents()) == {"101", "102"}


@pytest.mark.asyncio
async def test_node_sets_carry_the_call_kind_type_and_team(clean_environment, api):
    _workspace(api)
    await _sync()

    docs = await _documents()
    assert _node_sets(docs["101"]) == {
        "fathom:external",
        "fathom:type:Customer call",
        "fathom:team:Sales",
    }
    assert _node_sets(docs["102"]) == {
        "fathom:internal",
        "fathom:type:Customer call",
        "fathom:team:Sales",
    }
