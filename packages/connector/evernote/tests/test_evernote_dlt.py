"""End-to-end forget-on-delete through a real ``dlt`` pipeline (no Evernote creds).

The unit suite proves the connector *emits* the right rows. This proves dlt
actually *acts* on them: two real pipeline runs against a duckdb destination, where
run 2 emits a ``_deleted`` tombstone and the row must be physically gone from the
destination afterwards. Without this, a marker that dlt silently ignored would still
leave every unit test green.

Also asserts the resource-state cursor survives a real run, which is what makes
run 2 incremental rather than a second full ingest.
"""

import pytest

from cognee_community_connector_evernote.evernote import evernote_source

from .fake_evernote import FakeEvernoteStore

TOKEN = "S=s1:U=test:E=test:C=0:P=0cd:A=t:V=2:H=h:F=000:T=web&K=k&S=s"


@pytest.fixture
def dlt_pipeline(tmp_path):
    dlt = pytest.importorskip("dlt")
    pytest.importorskip("duckdb")

    pipeline = dlt.pipeline(
        pipeline_name="evernote_dlt_e2e",
        # The file stem becomes the duckdb catalog name; keeping it distinct from
        # dataset_name avoids "ambiguous reference to catalog or schema".
        destination=dlt.destinations.duckdb(str(tmp_path / "en_store.duckdb")),
        dataset_name="evernote",
    )
    yield pipeline


def _ids_in(pipeline, table):
    with pipeline.sql_client() as client:
        rows = client.execute_sql(f"SELECT id FROM {table}")
    return sorted(row[0] for row in rows)


def test_tombstone_removes_the_row_from_a_real_dlt_destination(dlt_pipeline):
    store = FakeEvernoteStore(notebooks={"nb-1": "Work"})
    store.add_note("note-a", "<div>alpha body</div>", notebook_guid="nb-1")
    store.add_note("note-b", "<div>bravo body</div>", notebook_guid="nb-1")

    dlt_pipeline.run(evernote_source(TOKEN, store=store))
    assert _ids_in(dlt_pipeline, "evernote_notes") == ["note-a", "note-b"]

    # note-b is permanently deleted upstream. The connector emits a hard-delete
    # marker; dlt's merge must remove the row rather than leave it behind.
    store.expunge_note("note-b")
    dlt_pipeline.run(evernote_source(TOKEN, store=store))

    assert _ids_in(dlt_pipeline, "evernote_notes") == ["note-a"]


def test_trashing_a_note_also_removes_the_row(dlt_pipeline):
    store = FakeEvernoteStore()
    store.add_note("note-a", "<div>alpha</div>")
    store.add_note("note-b", "<div>bravo</div>")
    dlt_pipeline.run(evernote_source(TOKEN, store=store))
    assert len(_ids_in(dlt_pipeline, "evernote_notes")) == 2

    # Moving to Trash is a delete from the user's point of view — Evernote's Trash
    # is a real notebook, so the row must not survive a plain re-sync.
    store.trash_note("note-b")
    dlt_pipeline.run(evernote_source(TOKEN, store=store))

    assert _ids_in(dlt_pipeline, "evernote_notes") == ["note-a"]


def test_edited_note_updates_in_place_without_duplicating(dlt_pipeline):
    store = FakeEvernoteStore()
    store.add_note("note-a", "<div>first draft</div>")
    dlt_pipeline.run(evernote_source(TOKEN, store=store))

    store.edit_note("note-a", "<div>second draft</div>")
    dlt_pipeline.run(evernote_source(TOKEN, store=store))

    assert _ids_in(dlt_pipeline, "evernote_notes") == ["note-a"], "merge, not append"
    with dlt_pipeline.sql_client() as client:
        contents = client.execute_sql("SELECT content FROM evernote_notes")
    assert [row[0] for row in contents] == ["second draft"]


def test_unchanged_notes_are_not_re_written_on_a_second_run(dlt_pipeline):
    """The cursor must make run 2 read *only* the delta from the API."""
    store = FakeEvernoteStore()
    store.add_note("note-a", "<div>alpha</div>")
    store.add_note("note-b", "<div>bravo</div>")
    dlt_pipeline.run(evernote_source(TOKEN, store=store))

    before = len([c for c in store.calls if c[0] == "get_note_content"])
    store.calls.clear()

    # Nothing changed upstream: no chunk requests at all, so no content fetches.
    dlt_pipeline.run(evernote_source(TOKEN, store=store))
    assert [c for c in store.calls if c[0] == "get_note_content"] == []
    assert len(store.calls) <= 2, "an idle re-sync should only stat the sync state"

    store.edit_note("note-b", "<div>bravo edited</div>")
    store.calls.clear()
    dlt_pipeline.run(evernote_source(TOKEN, store=store))

    fetched = [c[1][0] for c in store.calls if c[0] == "get_note_content"]
    assert fetched == ["note-b"], "only the changed note is re-fetched"
    assert before > 0
