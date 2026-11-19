"""circleci_source end to end: a real dlt pipeline into a temp sqlite destination."""

import dlt
import pytest
from cognee.tasks.ingestion import dlt_utils
from cognee.tasks.ingestion.dlt_utils import document_source_tag

from cognee_community_connector_circleci import CircleCIProjectNotFoundError, circleci_source
from cognee_community_connector_circleci.circleci import CIRCLECI_TABLE_NAME

SLUG = "gh/rokadepiyush49-rgb/cognee-circleci-fixture"
LISTING = f"/project/{SLUG}/pipeline"
OTHER = "gh/acme/other"

MAIN = "2ddad363-2d2e-4476-9cd1-aed100049957"
SLOW_RERUN = "4c2bfb04-d2c8-4449-a78a-ea76aae951c9"

OTHER_PIPELINE = {
    "id": "other-77",
    "number": 77,
    "project_slug": OTHER,
    "state": "created",
    "created_at": "2026-10-07T20:00:00.000Z",
    "vcs": {"branch": "main"},
}


def _pipeline(tmp_path):
    tmp_path.mkdir(parents=True, exist_ok=True)
    db_path = (tmp_path / "circleci.db").as_posix()
    return dlt.pipeline(
        pipeline_name="circleci_test",
        destination=dlt.destinations.sqlalchemy(f"sqlite:///{db_path}"),
        dataset_name="circleci_ds",
        pipelines_dir=str(tmp_path / "state"),
    )


def _run(pipeline, service, slugs=(SLUG,), **kwargs):
    source = circleci_source(project_slugs=list(slugs), service=service, **kwargs)
    pipeline.run(source)
    return source


def _rows(pipeline, table=CIRCLECI_TABLE_NAME):
    """Return {id: {title, content, url}} from the destination table.

    Reads positionally (the SELECT fixes the column order) since dlt's sqlalchemy
    cursor exposes a SQLAlchemy Result without DB-API ``description``.
    """
    with (
        pipeline.sql_client() as client,
        client.execute_query(f"SELECT id, title, content, url FROM {table}") as cursor,
    ):
        rows = cursor.fetchall()
    return {row[0]: {"title": row[1], "content": row[2], "url": row[3]} for row in rows}


def _with_other_project(session):
    session.queue(f"/project/{OTHER}/pipeline", (200, {"items": [OTHER_PIPELINE]}))
    session.queue("/pipeline/other-77/workflow", (200, {"items": []}))
    return session


def _causes(exc):
    while exc is not None:
        yield exc
        exc = exc.__cause__ or exc.__context__


def test_source_is_a_merge_resource_tagged_as_a_document_source():
    resource = circleci_source(project_slugs=[SLUG], token="test-token")
    columns = resource.compute_table_schema()["columns"]

    assert resource.name == CIRCLECI_TABLE_NAME
    assert resource.write_disposition == "merge"
    assert columns["id"]["primary_key"] is True
    assert columns["_deleted"]["hard_delete"] is True
    assert document_source_tag(resource) == "circleci"
    assert getattr(resource, dlt_utils.PIPELINE_SCOPE_ATTR) == CIRCLECI_TABLE_NAME
    assert resource.cognee_sync_stats == {}


def test_resource_name_names_the_resource_and_its_pipeline_scope():
    resource = circleci_source(project_slugs=[SLUG], token="t", resource_name="circleci_conn_1")

    assert resource.name == "circleci_conn_1"
    assert getattr(resource, dlt_utils.PIPELINE_SCOPE_ATTR) == "circleci_conn_1"


def test_first_run_loads_every_pipeline(tmp_path, fake_session):
    pipeline = _pipeline(tmp_path)

    source = _run(pipeline, fake_session)

    rows = _rows(pipeline)
    assert len(rows) == 5
    assert rows[MAIN]["title"] == f"{SLUG} pipeline #1 on main: failed"
    assert "KeyError: 'timeout'" in rows[MAIN]["content"]
    assert rows[MAIN]["url"].endswith("/github/rokadepiyush49-rgb/cognee-circleci-fixture/1")
    assert source.cognee_sync_stats["scanned"] == 5
    assert source.cognee_sync_stats["failed"] == 0


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


def test_two_sources_in_one_dataset_keep_each_others_pipelines(tmp_path, session_for):
    pipeline = _pipeline(tmp_path)

    _run(pipeline, session_for("index.json"), resource_name="circleci_a")
    _run(pipeline, _with_other_project(session_for()), (OTHER,), resource_name="circleci_b")
    _run(pipeline, _with_other_project(session_for()), (OTHER,), resource_name="circleci_b")

    assert len(_rows(pipeline, "circleci_a")) == 5
    assert list(_rows(pipeline, "circleci_b")) == ["other-77"]


def test_two_sources_sharing_a_resource_name_forget_each_others_projects(tmp_path, session_for):
    # Why resource_name exists: with one shared state, B sees A's project as
    # dropped from project_slugs.
    pipeline = _pipeline(tmp_path)

    _run(pipeline, session_for("index.json"))
    _run(pipeline, _with_other_project(session_for()), (OTHER,))

    assert list(_rows(pipeline)) == ["other-77"]


def test_project_that_404s_fails_the_run_and_keeps_its_rows(tmp_path, session_for):
    pipeline = _pipeline(tmp_path)
    _run(pipeline, session_for("index.json"))
    gone = session_for("index.json")
    gone.queue(LISTING, (404, {"message": "Project not found"}))

    with pytest.raises(Exception) as excinfo:
        _run(pipeline, gone)

    assert any(isinstance(e, CircleCIProjectNotFoundError) for e in _causes(excinfo.value))
    assert len(_rows(pipeline)) == 5


def test_check_active_is_called_and_can_stop_the_run(tmp_path, fake_session):
    calls = []
    _run(_pipeline(tmp_path / "ok"), fake_session, check_active=lambda: calls.append(1))
    # Before every request, and around every row handed to dlt.
    assert len(calls) > len(fake_session.calls)

    def revoked():
        raise PermissionError("connection revoked")

    with pytest.raises(Exception) as excinfo:
        _run(_pipeline(tmp_path / "revoked"), fake_session, check_active=revoked)
    assert any(isinstance(e, PermissionError) for e in _causes(excinfo.value))


def test_session_is_an_alias_of_service(tmp_path, fake_session):
    pipeline = _pipeline(tmp_path)

    pipeline.run(circleci_source(project_slugs=[SLUG], session=fake_session))

    assert len(_rows(pipeline)) == 5
    with pytest.raises(ValueError, match="not both"):
        circleci_source(project_slugs=[SLUG], service=fake_session, session=fake_session)


def test_token_is_an_argument_only(monkeypatch):
    # CIRCLECI_TOKEN is the example's business, not the connector's.
    monkeypatch.setenv("CIRCLECI_TOKEN", "from-env")
    with pytest.raises(ValueError, match="token"):
        circleci_source(project_slugs=[SLUG])

    assert circleci_source(project_slugs=[SLUG], token="t").name == CIRCLECI_TABLE_NAME


def test_old_cognee_is_refused(monkeypatch):
    monkeypatch.setattr(dlt_utils, "DOCUMENT_SYNC_VERSION", 0)

    with pytest.raises(RuntimeError, match="DOCUMENT_SYNC_VERSION"):
        circleci_source(project_slugs=[SLUG], token="t")


@pytest.mark.parametrize("project_slugs", [SLUG, []])
def test_project_slugs_must_be_a_non_empty_list(project_slugs):
    with pytest.raises(ValueError, match="project_slugs"):
        circleci_source(project_slugs=project_slugs, token="test-token")


def test_the_sync_token_is_masked_in_stored_failure_output(tmp_path, fake_session):
    # Stand-in token that appears in a recorded failure message ("KeyError: 'timeout'").
    pipeline = _pipeline(tmp_path)

    pipeline.run(circleci_source(project_slugs=[SLUG], token="timeout", service=fake_session))

    content = _rows(pipeline)[MAIN]["content"]
    assert "KeyError: '[REDACTED]'" in content
    assert "'timeout'" not in content
