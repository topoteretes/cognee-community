"""End-to-end test: the Lever connector through the real ``cognee.add()`` pipeline,
with the Lever API mocked (no live API key).

Verifies, at the add()/Data-record layer (no cognify(), so no LLM calls):
  - the first sync creates one Data record per posting
  - rows are routed through document mode (source="lever"), so cognify would
    chunk + LLM-extract them rather than schema-wrap them
  - an incremental re-sync only re-processes the changed posting — an
    unchanged posting keeps its data_id
  - a posting deleted in Lever is forgotten (orphan_cleanup fires)
"""

import cognee
import pytest
import pytest_asyncio
from cognee.modules.data.methods import get_authorized_existing_datasets
from cognee.modules.data.methods.get_dataset_data import get_dataset_data
from cognee.modules.users.methods import get_default_user
from conftest import FakeLeverSession, posting

from cognee_community_connector_lever import lever_source

DATASET_NAME = "lever_integration_test"
T0 = 1_800_000_000_000


@pytest_asyncio.fixture
async def clean_environment(tmp_path, monkeypatch):
    # add() never calls the LLM, but cognee's startup connection check would.
    monkeypatch.setenv("COGNEE_SKIP_CONNECTION_TEST", "true")

    cognee.config.data_root_directory(str(tmp_path / "data"))
    cognee.config.system_root_directory(str(tmp_path / "system"))
    cognee.config.set_relational_db_config({"db_provider": "sqlite"})

    await cognee.prune.prune_data()
    await cognee.prune.prune_system(metadata=True)
    yield
    await cognee.prune.prune_data()
    await cognee.prune.prune_system(metadata=True)


async def _lever_data():
    user = await get_default_user()
    datasets = await get_authorized_existing_datasets(
        user=user, permission_type="write", datasets=[DATASET_NAME]
    )
    if not datasets:
        return {}
    return {
        d.external_metadata["external_id"]: d
        for d in await get_dataset_data(datasets[0].id)
        if isinstance(d.external_metadata, dict) and d.external_metadata.get("source") == "lever"
    }


async def _sync(session, now_ms):
    await cognee.add(
        lever_source(session=session, clock=lambda: now_ms),
        dataset_name=DATASET_NAME,
        primary_key="id",
        write_disposition="merge",
        max_rows_per_table=0,
    )


@pytest.mark.asyncio
async def test_incremental_resync_and_deletion_propagate(clean_environment):
    from cognee.tasks.ingestion.dlt_utils import is_dlt_sourced

    await _sync(
        FakeLeverSession(
            postings=[
                posting("p1", updated_at=T0 - 10, text="Backend Engineer"),
                posting("p2", updated_at=T0 - 10, text="Data Scientist"),
                posting("p3", updated_at=T0 - 10, text="SRE"),
            ]
        ),
        T0,
    )
    initial = await _lever_data()
    assert set(initial) == {"posting:p1", "posting:p2", "posting:p3"}
    assert all(not is_dlt_sourced(d.external_metadata) for d in initial.values())
    assert initial["posting:p1"].external_metadata["url"] == "https://jobs.lever.co/acme/p1"

    # Incremental run: p1 edited, p2 deleted in Lever, p3 untouched (not in delta).
    await _sync(
        FakeLeverSession(
            postings=[posting("p1", updated_at=T0 + 10, text="Senior Backend Engineer")],
            deleted_postings=[{"id": "p2", "deletedAt": T0 + 20}],
        ),
        T0 + 60_000,
    )
    final = await _lever_data()

    assert "posting:p2" not in final  # forgotten
    assert final["posting:p1"].id != initial["posting:p1"].id  # new content version
    assert final["posting:p3"].id == initial["posting:p3"].id  # untouched, not re-created
    assert len(final) == 2
