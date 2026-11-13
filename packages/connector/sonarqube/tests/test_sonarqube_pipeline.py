"""A real dlt pipeline merging SonarQube documents into a temp sqlite destination."""

import dlt
from cognee.tasks.ingestion.dlt_utils import pipeline_name_for_source
from fake_sonarqube import FakeSonarQubeSession, issue

from cognee_community_connector_sonarqube import sonarqube_source


def _pipeline(tmp_path, name):
    db_path = (tmp_path / "sonarqube.db").as_posix()
    return dlt.pipeline(
        pipeline_name=name,
        destination=dlt.destinations.sqlalchemy(f"sqlite:///{db_path}"),
        dataset_name="sonarqube_ds",
        pipelines_dir=str(tmp_path / "state"),
    )


def _source(session, **source_kwargs):
    kwargs = {
        "token": "tok",
        "base_url": "https://sonarqube.example.com",
        "project_keys": ["proj"],
        "session": session,
        **source_kwargs,
    }
    return sonarqube_source(**kwargs)


def _run_pipeline(tmp_path, session, **source_kwargs):
    source = _source(session, **source_kwargs)
    pipeline = _pipeline(tmp_path, pipeline_name_for_source(source, "sonarqube_ds"))
    pipeline.run(source)
    return pipeline


def _read_documents(pipeline, table="sonarqube_documents"):
    with (
        pipeline.sql_client() as client,
        client.execute_query(f"SELECT id, content FROM {table}") as cursor,
    ):
        return {row[0]: row[1] for row in cursor.fetchall()}


def test_pipeline_first_run_writes_every_document(tmp_path):
    session = FakeSonarQubeSession(
        projects={"proj": {"name": "Proj", "issues": [issue("A"), issue("B")]}}
    )
    documents = _read_documents(_run_pipeline(tmp_path, session))
    assert {d.split(":")[-1] for d in documents} == {"A", "B"}
    assert "Do not use this pattern." in next(iter(documents.values()))


def test_pipeline_unchanged_row_survives_a_zero_row_run(tmp_path):
    session = FakeSonarQubeSession(projects={"proj": {"name": "Proj", "issues": [issue("A")]}})
    _run_pipeline(tmp_path, session)
    pipeline = _run_pipeline(tmp_path, session)
    documents = _read_documents(pipeline)
    assert len(documents) == 1


def test_pipeline_deletion_removes_the_row_from_the_destination(tmp_path):
    session = FakeSonarQubeSession(
        projects={"proj": {"name": "Proj", "issues": [issue("A"), issue("B")]}}
    )
    _run_pipeline(tmp_path, session)

    session.projects["proj"]["issues"] = [issue("A")]
    pipeline = _run_pipeline(tmp_path, session)

    documents = _read_documents(pipeline)
    assert not any(doc_id.endswith(":B") for doc_id in documents)
    assert any(doc_id.endswith(":A") for doc_id in documents)


def test_pipeline_exception_fails_the_run_without_writing_partial_rows(tmp_path):
    session = FakeSonarQubeSession(
        projects={"proj": {"name": "Proj", "issues": [issue("A"), issue("B")]}}
    )
    _run_pipeline(tmp_path, session)

    session.script("/api/issues/search", 403, {"errors": [{"msg": "no browse"}]})
    try:
        _run_pipeline(tmp_path, session)
        raise AssertionError("expected the pipeline run to fail")
    except Exception:
        pass

    # Nothing changed: both documents from the first run are still there, untouched.
    source = _source(session)
    pipeline = _pipeline(tmp_path, pipeline_name_for_source(source, "sonarqube_ds"))
    documents = _read_documents(pipeline)
    assert {d.split(":")[-1] for d in documents} == {"A", "B"}
