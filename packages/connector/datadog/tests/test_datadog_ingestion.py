"""The Datadog source through the real cognee.add() pipeline, with the API faked.

remember() runs this same add() step before cognify(), which needs an LLM, so these
tests stop at the stored Data records.
"""

from urllib.parse import unquote, urlsplit
from urllib.request import url2pathname

import cognee
import pytest
from cognee.modules.data.methods import get_authorized_existing_datasets
from cognee.modules.data.methods.get_dataset_data import get_dataset_data
from cognee.modules.users.methods import get_default_user
from fake_datadog import API_KEY, APP_KEY, FakeDatadog, client_for

from cognee_community_connector_datadog import datadog_source

DATASET = "datadog_integration_test"
TABLE = "datadog_test"
ALL = {"monitor:1", "dashboard:abc-123", "notebook:10", "slo:slo1", "incident:inc-1"}


def _org(site: str = "datadoghq.com") -> FakeDatadog:
    fake = FakeDatadog(site=site)
    fake.add_monitor(1, "Checkout latency", "Runbook: roll back payments-api.")
    fake.add_dashboard("abc-123", "Checkout")
    fake.add_notebook(10, "Postmortem: checkout outage", "The deploy skipped the canary.")
    fake.add_slo("slo1", "Checkout availability", monitor_ids=[1])
    fake.add_incident("inc-1", 7, "Checkout down in EU")
    return fake


async def _sync(fake: FakeDatadog, resource_name: str = TABLE, site: str = "datadoghq.com"):
    source = datadog_source(service=client_for(fake, site=site), resource_name=resource_name)
    await cognee.add(
        source,
        dataset_name=DATASET,
        primary_key="id",
        write_disposition="merge",
        max_rows_per_table=0,
    )
    return source


def _local_path(location: str) -> str:
    """The file a ``file://`` location points at, on Windows too (``file:///C:/a%20b``)."""
    if not location.startswith("file://"):
        return location
    return url2pathname(unquote(urlsplit(location).path))


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
        and data.system_metadata.get("source") == "datadog"
    }


@pytest.mark.asyncio
async def test_a_first_sync_stores_tagged_documents_and_a_quiet_one_changes_nothing(
    clean_environment,
):
    fake = _org()
    await _sync(fake)
    first = await _documents()

    assert set(first) == ALL
    assert {doc.system_metadata["table_name"] for doc in first.values()} == {TABLE}

    quiet = await _sync(fake)
    assert quiet.cognee_sync_stats["skipped"] == 5
    assert {k: d.id for k, d in (await _documents()).items()} == {k: d.id for k, d in first.items()}


@pytest.mark.asyncio
async def test_an_edited_runbook_replaces_only_that_monitors_document(clean_environment):
    fake = _org()
    await _sync(fake)
    before = {k: d.id for k, d in (await _documents()).items()}

    fake.monitors["1"]["message"] = "Runbook: roll back, then page payments."
    await _sync(fake)
    after = {k: d.id for k, d in (await _documents()).items()}

    assert set(after) == set(before)
    assert after["monitor:1"] != before["monitor:1"]
    assert {k: v for k, v in after.items() if k != "monitor:1"} == {
        k: v for k, v in before.items() if k != "monitor:1"
    }


@pytest.mark.asyncio
async def test_deleted_and_trashed_items_are_forgotten_and_a_restore_brings_one_back(
    clean_environment,
):
    fake = _org()
    await _sync(fake)

    del fake.monitors["1"]
    del fake.notebooks["10"]
    fake.trash_dashboard("abc-123")
    await _sync(fake)
    assert set(await _documents()) == {"slo:slo1", "incident:inc-1"}

    fake.restore_dashboard("abc-123")
    await _sync(fake)
    assert set(await _documents()) == {"slo:slo1", "incident:inc-1", "dashboard:abc-123"}


@pytest.mark.asyncio
async def test_a_rate_limited_kind_resumes_without_losing_documents(clean_environment):
    fake = _org()
    fake.statuses["list:incidents"] = 429
    cut = await _sync(fake)
    assert cut.cognee_sync_stats["failed_rate_limit_incidents"] == 1
    assert set(await _documents()) == ALL - {"incident:inc-1"}

    del fake.statuses["list:incidents"]
    done = await _sync(fake)
    assert done.cognee_sync_stats["failed"] == 0
    assert set(await _documents()) == ALL


@pytest.mark.asyncio
async def test_the_keys_never_reach_stored_documents(clean_environment):
    fake = _org()
    source = await _sync(fake)
    assert API_KEY not in repr(source) and APP_KEY not in repr(source)

    for doc in (await _documents()).values():
        assert API_KEY not in repr(doc)
        with open(_local_path(doc.raw_data_location), encoding="utf-8") as stored:
            text = stored.read()
        assert API_KEY not in text and APP_KEY not in text


@pytest.mark.asyncio
async def test_two_datadog_orgs_in_one_dataset_stay_apart(clean_environment):
    us, eu = _org(), FakeDatadog(site="datadoghq.eu")
    eu.add_monitor(100, "EU checkout latency")
    eu.add_monitor(101, "EU disk full")

    await _sync(us, "datadog_us")
    await _sync(eu, "datadog_eu", site="datadoghq.eu")
    both = ALL | {"monitor:100", "monitor:101"}
    assert set(await _documents()) == both

    del eu.monitors["101"]
    await _sync(eu, "datadog_eu", site="datadoghq.eu")
    await _sync(us, "datadog_us")

    assert set(await _documents()) == both - {"monitor:101"}
