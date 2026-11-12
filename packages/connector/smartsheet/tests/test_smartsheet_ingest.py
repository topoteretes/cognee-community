"""Real dlt.pipeline run into sqlite: initial sync, idempotent re-run, failure propagation."""

import pytest
from fake_smartsheet import FakeSmartsheetSession, _BoomSession, cell, column, row, sheet

from cognee_community_connector_smartsheet import smartsheet_source


@pytest.fixture
def dlt_mod():
    return pytest.importorskip("dlt")


def _basic_sheet():
    columns = [column(1, "Task", "TEXT_NUMBER", primary=True)]
    rows = [row(1, 1, [cell(1, display_value="Draft the launch plan")])]
    return sheet(1, name="Launch Plan", columns=columns, rows=rows)


def _run_sync(dlt, tmp_path, run_name, session):
    db_path = (tmp_path / f"{run_name}.db").as_posix()
    pipeline = dlt.pipeline(
        pipeline_name=run_name,
        destination=dlt.destinations.sqlalchemy(f"sqlite:///{db_path}"),
        dataset_name="smartsheet_ds",
        pipelines_dir=str(tmp_path / "state"),
    )
    pipeline.run(
        smartsheet_source(
            api_token="tok",
            sheet_ids=[1],
            include_comments=False,
            include_attachments=False,
            session=session,
        )
    )
    return pipeline


def _read_documents(pipeline):
    with (
        pipeline.sql_client() as client,
        client.execute_query("SELECT id, title, content FROM smartsheet_documents") as cursor,
    ):
        rows = cursor.fetchall()
    return {r[0]: {"id": r[0], "title": r[1], "content": r[2]} for r in rows}


def test_initial_sync_stages_summary_and_row_documents(dlt_mod, tmp_path):
    session = FakeSmartsheetSession(sheets={1: _basic_sheet()})
    pipeline = _run_sync(dlt_mod, tmp_path, "smartsheet_initial", session)
    rows = _read_documents(pipeline)
    assert "smartsheet:com:1:summary" in rows
    assert "smartsheet:com:1:row:1" in rows
    assert "Draft the launch plan" in rows["smartsheet:com:1:row:1"]["content"]


def test_rerun_with_no_changes_is_idempotent(dlt_mod, tmp_path):
    session1 = FakeSmartsheetSession(sheets={1: _basic_sheet()})
    _run_sync(dlt_mod, tmp_path, "smartsheet_idempotent", session1)

    session2 = FakeSmartsheetSession(sheets={1: _basic_sheet()})
    pipeline = _run_sync(dlt_mod, tmp_path, "smartsheet_idempotent", session2)
    rows = _read_documents(pipeline)
    assert len(rows) == 2  # summary + 1 row, not duplicated


def test_api_exception_propagates_and_fails_the_run(dlt_mod, tmp_path):
    db_path = (tmp_path / "smartsheet_boom.db").as_posix()
    pipeline = dlt_mod.pipeline(
        pipeline_name="smartsheet_boom",
        destination=dlt_mod.destinations.sqlalchemy(f"sqlite:///{db_path}"),
        dataset_name="smartsheet_ds",
        pipelines_dir=str(tmp_path / "state"),
    )
    with pytest.raises(Exception):  # noqa: B017 - dlt wraps the source error in PipelineStepFailed
        pipeline.run(smartsheet_source(api_token="tok", sheet_ids=[1], session=_BoomSession()))
