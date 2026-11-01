"""End-to-end through cognee's real ``add`` path (no LLM, no network).

``cognee.add`` runs ``resolve_dlt_sources`` and the post-commit ``orphan_cleanup``; only
``cognify`` (graph extraction) needs an LLM, so it is not exercised here. This is the
evidence that tombstones really remove records from cognee's stores.
"""

import asyncio

import cognee
import pytest
from conftest import FakeOutreach


@pytest.fixture
def cognee_env(tmp_path, monkeypatch):
    monkeypatch.setenv("COGNEE_SKIP_CONNECTION_TEST", "true")
    monkeypatch.setenv("DLT_DATA_DIR", str(tmp_path / "dlt"))  # isolate dlt pipeline state
    cognee.config.system_root_directory(str(tmp_path / "cognee_system"))
    cognee.config.data_root_directory(str(tmp_path / "cognee_data"))


async def _external_ids(dataset_name):
    from cognee.modules.data.methods import get_authorized_existing_datasets
    from cognee.modules.data.methods.get_dataset_data import get_dataset_data
    from cognee.modules.users.methods import get_default_user

    user = await get_default_user()
    (dataset,) = await get_authorized_existing_datasets(
        user=user, permission_type="read", datasets=[dataset_name]
    )
    return {
        (d.external_metadata or {}).get("external_id") for d in await get_dataset_data(dataset.id)
    }


def test_add_ingests_documents_and_forgets_deleted_prospect(cognee_env, make_source):
    fake = FakeOutreach()
    kwargs = {
        "dataset_name": "outreach_it",
        "primary_key": "id",
        "write_disposition": "merge",
        "max_rows_per_table": 0,
    }
    scope = {"resources": ["prospects"]}

    async def scenario():
        await cognee.add(make_source(fake, **scope), **kwargs)
        first = await _external_ids("outreach_it")
        await cognee.add(make_source(fake, **scope), **kwargs)  # unchanged re-sync
        unchanged = await _external_ids("outreach_it")
        fake.remove("/prospects", 3)  # deleted upstream; only the id sweep can see it
        await cognee.add(make_source(fake, **scope), **kwargs)
        return first, unchanged, await _external_ids("outreach_it")

    first, unchanged, after_delete = asyncio.run(scenario())

    prospects = {f"outreach:prospect:{i}" for i in range(1, 6)}
    memberships = {f"outreach:sequence_state:{i}" for i in range(1, 5)}
    assert first == prospects | memberships
    assert unchanged == first
    assert after_delete == first - {"outreach:prospect:3"}
