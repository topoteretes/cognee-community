"""End-to-end through cognee's real ``add`` path (no LLM, no network).

``cognee.add`` runs ``resolve_dlt_sources`` and the post-commit ``orphan_cleanup``; only
``cognify`` (graph extraction) needs an LLM, so it is not exercised here.
"""

import asyncio

import cognee
import pytest
from conftest import FakeDeel


@pytest.fixture
def cognee_env(tmp_path, monkeypatch):
    monkeypatch.setenv("COGNEE_SKIP_CONNECTION_TEST", "true")
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


def test_add_ingests_documents_and_forgets_deleted_contract(cognee_env, make_source):
    fake = FakeDeel()
    kwargs = {
        "dataset_name": "deel_it",
        "primary_key": "id",
        "write_disposition": "merge",
        "max_rows_per_table": 0,
    }

    async def scenario():
        await cognee.add(make_source(fake), **kwargs)
        first = await _external_ids("deel_it")

        await cognee.add(make_source(fake), **kwargs)  # unchanged re-sync: nothing added or lost
        unchanged = await _external_ids("deel_it")

        del fake.contracts[2]  # c3 deleted upstream
        await cognee.add(make_source(fake), **kwargs)
        return first, unchanged, await _external_ids("deel_it")

    first, unchanged, after_delete = asyncio.run(scenario())

    assert first == {f"deel:contract:c{i}" for i in range(1, 6)} | {
        f"deel:person:p{i}" for i in range(1, 4)
    }
    assert unchanged == first
    assert after_delete == first - {"deel:contract:c3"}
