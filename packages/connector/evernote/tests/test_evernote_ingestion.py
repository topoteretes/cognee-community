"""End-to-end: the Evernote connector through the real ``cognee.add()`` pipeline.

Evernote's Thrift API is fully mocked (``tests/fake_evernote.py``), so this needs
no credentials, no network and no LLM — ``add()`` never calls one.

Verifies the issue's acceptance criteria at the Data-record layer:
  - the initial sync creates one Data record per in-scope note;
  - note rows are routed through *document mode* (``source="evernote"``), so cognify
    would chunk + LLM-extract them rather than schema-wrap them;
  - an incremental re-sync only re-processes changed notes — an untouched note keeps
    its ``data_id`` and is not re-created;
  - a note deleted upstream is forgotten (cognee's ``orphan_cleanup`` fires).
"""

import cognee
import pytest
import pytest_asyncio
from cognee.modules.data.methods import get_authorized_existing_datasets
from cognee.modules.data.methods.get_dataset_data import get_dataset_data
from cognee.modules.users.methods import get_default_user

from cognee_community_connector_evernote.evernote import evernote_source

from .fake_evernote import FakeEvernoteStore

DATASET_NAME = "evernote_ingestion_test"
TOKEN = "S=s1:U=test:E=test:C=0:P=0cd:A=t:V=2:H=h:F=000:T=web&K=k&S=s"


@pytest_asyncio.fixture
async def clean_environment(tmp_path, monkeypatch):
    pytest.importorskip("dlt")

    # add() never calls the LLM (no cognify()), but cognee's startup connection
    # check would still try to reach one — skip it so this needs no credentials.
    monkeypatch.setenv("COGNEE_SKIP_CONNECTION_TEST", "true")

    # cognee always builds a dlt pipeline literally named "ingest_dlt_source", so
    # dlt's on-disk resource state (where the connector's USN cursor lives) would
    # otherwise leak between tests: a fresh store starting at a low USN would be
    # judged "already synced" and ingest nothing. Repoint dlt's pipelines dir so
    # each test starts from a genuinely empty pipeline state.
    import dlt

    monkeypatch.setitem(dlt.config, "runtime.pipelines_dir", str(tmp_path / "dlt_pipelines"))

    # get_dlt_destination is @lru_cache'd, so without clearing it every test after
    # the first would write to the first test's database file.
    from cognee.tasks.ingestion.get_dlt_destination import get_dlt_destination

    get_dlt_destination.cache_clear()

    cognee.config.data_root_directory(str(tmp_path / "data"))
    cognee.config.system_root_directory(str(tmp_path / "system"))
    cognee.config.set_relational_db_config({"db_provider": "sqlite"})

    await cognee.prune.prune_data()
    await cognee.prune.prune_system(metadata=True)

    yield

    get_dlt_destination.cache_clear()
    await cognee.prune.prune_data()
    await cognee.prune.prune_system(metadata=True)


async def _evernote_sourced_data(dataset_name: str):
    user = await get_default_user()
    datasets = await get_authorized_existing_datasets(
        user=user, permission_type="write", datasets=[dataset_name]
    )
    if not datasets:
        return []
    return [
        d
        for d in await get_dataset_data(datasets[0].id)
        if isinstance(d.external_metadata, dict) and d.external_metadata.get("source") == "evernote"
    ]


async def _sync(store: FakeEvernoteStore, **kwargs):
    await cognee.add(
        evernote_source(TOKEN, store=store, **kwargs),
        dataset_name=DATASET_NAME,
        primary_key="id",
        write_disposition="merge",
        max_rows_per_table=0,
    )


@pytest.mark.asyncio
async def test_notes_are_ingested_and_routed_through_document_mode(clean_environment):
    store = FakeEvernoteStore(notebooks={"nb-1": "Work"})
    store.add_note(
        "note-a", "<div>Acme migrated to Postgres last quarter.</div>", notebook_guid="nb-1"
    )
    store.add_note("note-b", "<div>Bravo uses a weekly release train.</div>", notebook_guid="nb-1")

    await _sync(store)

    data = await _evernote_sourced_data(DATASET_NAME)
    assert len(data) == 2, "one Data record per note"
    assert {d.external_metadata["external_id"] for d in data} == {"note-a", "note-b"}

    # Rows tagged source="evernote" (not "dlt") mean is_dlt_sourced() is False, so
    # cognify would chunk and LLM-extract them instead of schema-wrapping. If this
    # regressed, notes would still ingest but contribute nothing to the graph.
    from cognee.tasks.ingestion.dlt_utils import is_dlt_sourced

    assert all(not is_dlt_sourced(d.external_metadata) for d in data)


@pytest.mark.asyncio
async def test_incremental_resync_only_touches_changed_notes(clean_environment):
    store = FakeEvernoteStore()
    store.add_note("note-a", "<div>alpha body</div>")
    store.add_note("note-b", "<div>bravo body</div>")
    store.add_note("note-c", "<div>charlie body</div>")
    await _sync(store)

    ingested = await _evernote_sourced_data(DATASET_NAME)
    initial = {d.external_metadata["external_id"]: d.id for d in ingested}
    assert set(initial) == {"note-a", "note-b", "note-c"}

    # note-a edited, note-b permanently deleted, note-c untouched.
    store.edit_note("note-a", "<div>alpha body, revised</div>")
    store.expunge_note("note-b")
    await _sync(store)

    reingested = await _evernote_sourced_data(DATASET_NAME)
    final = {d.external_metadata["external_id"]: d for d in reingested}

    assert "note-b" not in final, "a deleted note must be forgotten via orphan_cleanup"
    assert final["note-c"].id == initial["note-c"], "an unchanged note is not re-created"
    assert final["note-a"].id != initial["note-a"], "an edited note gets a new content hash"


@pytest.mark.asyncio
async def test_trashing_a_note_forgets_it(clean_environment):
    store = FakeEvernoteStore()
    store.add_note("note-a", "<div>alpha body</div>")
    store.add_note("note-b", "<div>bravo body</div>")
    await _sync(store)
    assert len(await _evernote_sourced_data(DATASET_NAME)) == 2

    # Evernote's Trash is a real notebook, so a naive re-sync would keep note-b.
    store.trash_note("note-b")
    await _sync(store)

    remaining = await _evernote_sourced_data(DATASET_NAME)
    assert [d.external_metadata["external_id"] for d in remaining] == ["note-a"]


@pytest.mark.asyncio
async def test_narrowing_the_notebook_scope_forgets_the_dropped_notes(clean_environment):
    store = FakeEvernoteStore(notebooks={"nb-1": "Work", "nb-2": "Personal"})
    store.add_note("note-a", "<div>work note</div>", notebook_guid="nb-1")
    store.add_note("note-b", "<div>personal note</div>", notebook_guid="nb-2")
    await _sync(store)
    assert len(await _evernote_sourced_data(DATASET_NAME)) == 2

    # The user narrows the ingest to Work only; the personal note must be forgotten
    # rather than lingering in memory forever.
    await _sync(store, notebook_guids=["nb-1"])

    remaining = await _evernote_sourced_data(DATASET_NAME)
    assert [d.external_metadata["external_id"] for d in remaining] == ["note-a"]


@pytest.mark.asyncio
async def test_only_selected_tags_are_ingested(clean_environment):
    store = FakeEvernoteStore()
    store.add_note("note-a", "<div>tagged research</div>", tags=["research"])
    store.add_note("note-b", "<div>untagged</div>")
    await _sync(store, tag_names=["research"])

    remaining = await _evernote_sourced_data(DATASET_NAME)
    assert [d.external_metadata["external_id"] for d in remaining] == ["note-a"]
