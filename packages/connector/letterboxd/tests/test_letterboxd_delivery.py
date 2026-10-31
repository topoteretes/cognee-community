"""A real dlt pipeline end to end (the pattern of telegram's delivery test).

Runs the source through a real dlt merge into SQLite and checks what cognee's
forget-on-delete will see. ``max_rows_per_table=0`` makes cognee compare
deletions against every stored row instead of a tail, so what these tests
assert about the physical table contents is exactly the diff cognee sees: a
deleted film disappears from the table, and nothing else moves.
"""

import dlt
import pytest
from dlt.destinations.exceptions import DatabaseUndefinedRelation
from dlt.pipeline.exceptions import PipelineStepFailed
from fake_letterboxd import (
    EDITED_GET_OUT_REVIEW,
    GET_OUT,
    GET_OUT_REVIEW,
    HEAT,
    MAD_MAX,
    PARASITE,
    WHIPLASH,
    default_export,
    with_edited_review,
    without_film,
    without_list,
    write_zip,
)

from cognee_community_connector_letterboxd import letterboxd_source

TABLE = "letterboxd_default"


@pytest.fixture(autouse=True)
def no_active_pipeline():
    """Iterating a resource outside a run uses the active pipeline's state, so
    make sure a pipeline left active by an earlier test cannot leak it in."""
    from dlt.common.configuration.container import Container
    from dlt.common.pipeline import PipelineContext

    Container()[PipelineContext].deactivate()
    yield
    Container()[PipelineContext].deactivate()


def _pipeline(tmp_path):
    return dlt.pipeline(
        pipeline_name="letterboxd_e2e",
        destination=dlt.destinations.sqlalchemy(f"sqlite:///{tmp_path / 'lb.db'}"),
        dataset_name="lb",
        pipelines_dir=str(tmp_path / "pipelines"),
    )


def _table(pipeline):
    with pipeline.sql_client() as client:
        try:
            return client.execute_sql(f"SELECT id, content FROM {TABLE} ORDER BY id")
        except DatabaseUndefinedRelation:  # nothing loaded yet
            return []


def test_e2e_merge_upserts_edits_and_physically_removes_deleted(tmp_path):
    pipeline = _pipeline(tmp_path)

    v1 = write_zip(tmp_path / "v1.zip")
    pipeline.run(letterboxd_source(export=v1))
    rows = _table(pipeline)
    assert [row[0] for row in rows] == sorted(
        [PARASITE, GET_OUT, HEAT, MAD_MAX, WHIPLASH, "list:favourites", "list:korean-cinema"]
    )

    # A newer export with one rewritten review: the same 7 ids, one new content.
    v2 = write_zip(tmp_path / "v2.zip", with_edited_review(default_export()))
    pipeline.run(letterboxd_source(export=v2))
    rows = _table(pipeline)
    assert len(rows) == 7
    get_out = next(row for row in rows if row[0] == GET_OUT)
    assert EDITED_GET_OUT_REVIEW in get_out[1]
    assert GET_OUT_REVIEW not in get_out[1]  # the old text was replaced

    # A newer export without Heat and without the Korean Cinema list: both are
    # physically gone, everything else stays.
    v3_files = without_list(without_film(default_export(), HEAT), "lists/Korean Cinema.csv")
    v3 = write_zip(tmp_path / "v3.zip", v3_files)
    pipeline.run(letterboxd_source(export=v3))
    ids = [row[0] for row in _table(pipeline)]
    assert HEAT not in ids and "list:korean-cinema" not in ids
    assert len(ids) == 5  # 4 films + the Favourites list


def test_e2e_the_same_export_twice_changes_nothing(tmp_path):
    pipeline = _pipeline(tmp_path)
    export = write_zip(tmp_path / "export.zip")
    pipeline.run(letterboxd_source(export=export))
    before = _table(pipeline)
    pipeline.run(letterboxd_source(export=export))
    assert _table(pipeline) == before  # unchanged rows keep their content hash


def test_e2e_a_replace_run_is_refused(tmp_path):
    pipeline = _pipeline(tmp_path)
    export = write_zip(tmp_path / "export.zip")
    with pytest.raises(PipelineStepFailed, match='needs write_disposition="merge"'):
        pipeline.run(letterboxd_source(export=export), write_disposition="replace")
    assert _table(pipeline) == [], "nothing was loaded by the refused run"
