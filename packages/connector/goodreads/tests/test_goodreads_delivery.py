"""A real dlt pipeline end to end (the pattern of telegram's delivery test).

Runs the source through a real dlt merge into SQLite and checks what cognee's
forget-on-delete will see. ``max_rows_per_table=0`` makes cognee compare
deletions against every stored row instead of a tail, so what these tests
assert about the physical table contents is exactly the diff cognee sees: a
deleted book disappears from the table, and nothing else moves.
"""

import dlt
import pytest
from dlt.destinations.exceptions import DatabaseUndefinedRelation
from dlt.pipeline.exceptions import PipelineStepFailed
from fake_goodreads import (
    DDIA,
    EDITED_DDIA_REVIEW,
    K_AND_R,
    default_rows,
    with_edited_review,
    without_book,
    write_export,
)

from cognee_community_connector_goodreads import goodreads_source

TABLE = "goodreads_default"


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
        pipeline_name="goodreads_e2e",
        destination=dlt.destinations.sqlalchemy(f"sqlite:///{tmp_path / 'gr.db'}"),
        dataset_name="gr",
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

    v1 = write_export(tmp_path / "v1.csv")
    pipeline.run(goodreads_source(export=v1))
    rows = _table(pipeline)
    assert len(rows) == 7
    assert sorted(row[0] for row in rows) == sorted(f"book:{id_}" for id_ in
                                                    ("1001", "1002", "1003", "1004", "1005",
                                                     "1006", "1007"))

    # A newer export with one rewritten review: the same 7 ids, one new content.
    v2 = write_export(tmp_path / "v2.csv", rows=with_edited_review(default_rows()))
    pipeline.run(goodreads_source(export=v2))
    rows = _table(pipeline)
    assert len(rows) == 7
    ddia = next(row for row in rows if row[0] == f"book:{DDIA}")
    assert EDITED_DDIA_REVIEW.split("<br/>")[0] in ddia[1]
    assert "A masterwork on systems design." not in ddia[1]  # the old text was replaced

    # A newer export without one book: it is physically gone, the rest stays.
    v3 = write_export(tmp_path / "v3.csv", rows=without_book(default_rows(), K_AND_R))
    pipeline.run(goodreads_source(export=v3))
    ids = [row[0] for row in _table(pipeline)]
    assert f"book:{K_AND_R}" not in ids
    assert len(ids) == 6


def test_e2e_the_same_export_twice_changes_nothing(tmp_path):
    pipeline = _pipeline(tmp_path)
    export = write_export(tmp_path / "export.csv")
    pipeline.run(goodreads_source(export=export))
    before = _table(pipeline)
    pipeline.run(goodreads_source(export=export))
    assert _table(pipeline) == before  # unchanged rows keep their content hash


def test_e2e_a_replace_run_is_refused(tmp_path):
    pipeline = _pipeline(tmp_path)
    export = write_export(tmp_path / "export.csv")
    with pytest.raises(PipelineStepFailed, match='needs write_disposition="merge"'):
        pipeline.run(goodreads_source(export=export), write_disposition="replace")
    assert _table(pipeline) == [], "nothing was loaded by the refused run"
