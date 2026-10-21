"""circleci_source end to end: a real dlt pipeline into a temp sqlite destination."""

import dlt
import pytest
from cognee.tasks.ingestion.dlt_utils import document_source_tag

from cognee_community_connector_circleci import circleci_source
from cognee_community_connector_circleci.circleci import CIRCLECI_TABLE_NAME

SLUG = "gh/rokadepiyush49-rgb/cognee-circleci-fixture"
LISTING = f"/project/{SLUG}/pipeline"

MAIN = "2ddad363-2d2e-4476-9cd1-aed100049957"
SLOW_RERUN = "4c2bfb04-d2c8-4449-a78a-ea76aae951c9"


def _pipeline(tmp_path):
    db_path = (tmp_path / "circleci.db").as_posix()
    return dlt.pipeline(
        pipeline_name="circleci_test",
        destination=dlt.destinations.sqlalchemy(f"sqlite:///{db_path}"),
        dataset_name="circleci_ds",
        pipelines_dir=str(tmp_path / "state"),
    )


def _run(pipeline, session):
    pipeline.run(circleci_source(project_slugs=[SLUG], session=session))


def _rows(pipeline):
    """Return {id: {title, content, url}} from the destination table.

    Reads positionally (the SELECT fixes the column order) since dlt's sqlalchemy
    cursor exposes a SQLAlchemy Result without DB-API ``description``.
    """
    with (
        pipeline.sql_client() as client,
        client.execute_query(
            f"SELECT id, title, content, url FROM {CIRCLECI_TABLE_NAME}"
        ) as cursor,
    ):
        rows = cursor.fetchall()
    return {row[0]: {"title": row[1], "content": row[2], "url": row[3]} for row in rows}


def test_source_is_a_merge_resource_tagged_as_a_document_source():
    resource = circleci_source(project_slugs=[SLUG], token="test-token")
    columns = resource.compute_table_schema()["columns"]

    assert resource.name == CIRCLECI_TABLE_NAME
    assert resource.write_disposition == "merge"
    assert columns["id"]["primary_key"] is True
    assert columns["_deleted"]["hard_delete"] is True
    assert document_source_tag(resource) == "circleci"


def test_first_run_loads_every_pipeline(tmp_path, fake_session):
    pipeline = _pipeline(tmp_path)

    _run(pipeline, fake_session)

    rows = _rows(pipeline)
    assert len(rows) == 5
    assert rows[MAIN]["title"] == f"{SLUG} pipeline #1 on main: failed"
    assert "KeyError: 'timeout'" in rows[MAIN]["content"]
    assert rows[MAIN]["url"].endswith("/github/rokadepiyush49-rgb/cognee-circleci-fixture/1")


def test_state_carries_over_between_runs(tmp_path, session_for):
    pipeline = _pipeline(tmp_path)

    _run(pipeline, session_for("index.json"))
    _run(pipeline, session_for("index.json", "slow-running/index.json"))
    running = _rows(pipeline)
    _run(pipeline, session_for("index.json", "slow-finished/index.json"))
    finished = _rows(pipeline)

    assert len(running) == len(finished) == 6
    assert running[SLOW_RERUN]["title"].endswith(": running")
    # merge upserted #6 in place with its final status.
    assert finished[SLOW_RERUN]["title"].endswith(": failed")


def test_project_that_404s_is_deleted_from_the_destination(tmp_path, session_for):
    pipeline = _pipeline(tmp_path)
    _run(pipeline, session_for("index.json"))
    gone = session_for("index.json")
    gone.queue(LISTING, (404, {"message": "Project not found"}))

    _run(pipeline, gone)

    assert _rows(pipeline) == {}


def test_token_is_required(monkeypatch):
    monkeypatch.delenv("CIRCLECI_TOKEN", raising=False)
    with pytest.raises(ValueError, match="CIRCLECI_TOKEN"):
        circleci_source(project_slugs=[SLUG])

    monkeypatch.setenv("CIRCLECI_TOKEN", "from-env")
    assert circleci_source(project_slugs=[SLUG]).name == CIRCLECI_TABLE_NAME


@pytest.mark.parametrize("project_slugs", [SLUG, []])
def test_project_slugs_must_be_a_non_empty_list(project_slugs):
    with pytest.raises(ValueError, match="project_slugs"):
        circleci_source(project_slugs=project_slugs, token="test-token")
