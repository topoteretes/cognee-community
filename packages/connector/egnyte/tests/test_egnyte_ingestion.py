"""End-to-end test: the Egnyte connector through the real cognee.add() pipeline,
with the Egnyte API faked (no credentials).

Verifies at the add()/Data-record layer (no cognify(), so no LLM calls):
  - the initial sync creates one Data record per supported file
  - rows are routed through document mode (source="egnyte"), so cognify would
    chunk and LLM-extract them rather than schema-wrap them
  - an incremental re-sync only re-processes changed files: an unchanged file
    keeps its data_id
  - a file deleted in Egnyte is forgotten (orphan_cleanup fires)
  - two Egnyte syncs with their own resource names can share a dataset
    without forgetting each other's files
  - the same Egnyte sync loaded into two datasets keeps a separate cursor for
    each, so alternating between them stays incremental
"""

import cognee
import pytest
import pytest_asyncio
from _fake_egnyte import FakeEgnyte
from cognee.modules.data.methods import get_authorized_existing_datasets
from cognee.modules.data.methods.get_dataset_data import get_dataset_data
from cognee.modules.users.methods import get_default_user
from cognee.tasks.ingestion.dlt_utils import is_dlt_sourced

from cognee_community_connector_egnyte import egnyte_source

DATASET_NAME = "egnyte_integration_test"
ROOT = "/Shared/Projects"


@pytest_asyncio.fixture
async def clean_environment(tmp_path, monkeypatch):
    pytest.importorskip("dlt")

    # add() never calls the LLM (no cognify()), but cognee's startup
    # connection check would still try to reach one.
    monkeypatch.setenv("COGNEE_SKIP_CONNECTION_TEST", "true")

    cognee.config.data_root_directory(str(tmp_path / "data"))
    cognee.config.system_root_directory(str(tmp_path / "system"))
    cognee.config.set_relational_db_config({"db_provider": "sqlite"})

    await cognee.prune.prune_data()
    await cognee.prune.prune_system(metadata=True)
    yield
    await cognee.prune.prune_data()
    await cognee.prune.prune_system(metadata=True)


async def _sync(fake, root=ROOT, resource_name="egnyte_files", dataset_name=DATASET_NAME):
    await cognee.add(
        egnyte_source(domain="acme", folder_paths=[root], resource_name=resource_name, client=fake),
        dataset_name=dataset_name,
        primary_key="id",
        write_disposition="merge",
        max_rows_per_table=0,
    )


def _source_metadata(data):
    """Where cognee stamps a row's source tag and external id.

    cognee 1.4 used ``external_metadata``; later releases moved these stamps to
    a separate, system-owned ``system_metadata`` column.
    """
    return getattr(data, "system_metadata", None) or data.external_metadata or {}


async def _egnyte_data(dataset_name=DATASET_NAME):
    user = await get_default_user()
    datasets = await get_authorized_existing_datasets(
        user=user, permission_type="write", datasets=[dataset_name]
    )
    if not datasets:
        return {}
    return {
        _source_metadata(d)["external_id"]: d
        for d in await get_dataset_data(datasets[0].id)
        if _source_metadata(d).get("source") == "egnyte"
    }


@pytest.mark.asyncio
async def test_incremental_resync_and_deletion_propagate(clean_environment):
    fake = FakeEgnyte()
    a = fake.put(f"{ROOT}/alice.txt", "Alice works at Acme.")
    b = fake.put(f"{ROOT}/bob.txt", "Bob works at Acme.")
    c = fake.put(f"{ROOT}/carol.txt", "Carol works at Acme.")

    await _sync(fake)

    initial = await _egnyte_data()
    assert set(initial) == {a, b, c}
    # Rows are tagged source="egnyte" (not "dlt"), so is_dlt_sourced() is False
    # and cognify picks TextDocument (chunk + LLM extract). If this regressed,
    # files would still ingest but contribute nothing to the graph.
    assert all(not is_dlt_sourced(_source_metadata(d)) for d in initial.values())

    # Incremental run: alice changes, bob is deleted, carol is left alone.
    fake.put(f"{ROOT}/alice.txt", "Alice now works at Globex.")
    fake.delete(f"{ROOT}/bob.txt")
    fake.calls.clear()

    await _sync(fake)

    final = await _egnyte_data()
    assert set(final) == {a, c}  # bob was forgotten
    assert final[a].id != initial[a].id  # new content, new content-hash data_id
    assert final[c].id == initial[c].id  # untouched, not re-created
    assert fake.downloads() == [f"{ROOT}/alice.txt"]


@pytest.mark.asyncio
async def test_two_egnyte_syncs_share_a_dataset_without_forgetting_each_other(
    clean_environment,
):
    fake = FakeEgnyte()
    project = fake.put(f"{ROOT}/plan.txt", "The plan is to ship in May.")
    legal_a = fake.put("/Shared/Legal/nda.txt", "The NDA runs for two years.")
    legal_b = fake.put("/Shared/Legal/msa.txt", "The MSA renews every year.")

    await _sync(fake, ROOT, "project_files")
    await _sync(fake, "/Shared/Legal", "legal_files")
    assert set(await _egnyte_data()) == {project, legal_a, legal_b}

    # A delete under Legal is forgotten, and the Projects file stays.
    fake.delete("/Shared/Legal/msa.txt")
    await _sync(fake, "/Shared/Legal", "legal_files")
    assert set(await _egnyte_data()) == {project, legal_a}

    # Re-running the Projects sync leaves the Legal file alone.
    await _sync(fake, ROOT, "project_files")
    assert set(await _egnyte_data()) == {project, legal_a}


@pytest.mark.asyncio
async def test_alternating_datasets_keep_separate_cursors(clean_environment):
    fake = FakeEgnyte()
    plan = fake.put(f"{ROOT}/plan.txt", "The plan is to ship in May.")

    await _sync(fake)
    await _sync(fake, dataset_name="egnyte_second_dataset")
    assert set(await _egnyte_data("egnyte_second_dataset")) == {plan}

    # Back to the first dataset: it resumes from its own cursor, so nothing
    # is listed or downloaded again.
    fake.calls.clear()
    await _sync(fake)
    assert fake.downloads() == []
    assert not fake.calls_of("list")
    assert set(await _egnyte_data()) == {plan}
