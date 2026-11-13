"""Tests for the prefect_source dlt resource and real dlt pipeline execution."""

import pytest
from cognee.tasks.ingestion.dlt_utils import DOCUMENT_SOURCE_ATTR, PIPELINE_SCOPE_ATTR
from fake_prefect import FakePrefect

from cognee_community_connector_prefect import prefect_source

TABLE = "prefect_test"


def _workspace() -> FakePrefect:
    fake = FakePrefect()
    fake.add_flow("f1", "Data Pipeline")
    fake.add_deployment("d1", "Nightly Deployment", "f1")
    fake.add_flow_run("r1", "Run 1", "f1", deployment_id="d1")
    return fake


def test_resource_merge_and_document_tagging():
    resource = prefect_source(service=FakePrefect(), resource_name=TABLE)

    assert resource.name == TABLE
    assert getattr(resource, DOCUMENT_SOURCE_ATTR) == "prefect"
    assert getattr(resource, PIPELINE_SCOPE_ATTR) == TABLE
    hints = resource.compute_table_schema()
    assert hints["write_disposition"] == "merge"
    assert hints["columns"]["_deleted"]["hard_delete"] is True
    assert resource.cognee_sync_stats == {}


@pytest.mark.parametrize(
    "kwargs",
    [
        {"resource_name": ""},
        {"resource_name": "   "},
        {"resource_name": TABLE, "include": ()},
        {"resource_name": TABLE, "include": ("flows", "invalid_kind")},
        {"resource_name": TABLE, "base_url": "invalid-url"},
    ],
)
def test_bad_arguments_rejected_at_construction(kwargs):
    with pytest.raises(ValueError):
        prefect_source(**kwargs)


def _pipeline(tmp_path):
    import dlt

    return dlt.pipeline(
        pipeline_name="prefect_test_pipeline",
        destination=dlt.destinations.sqlalchemy(f"sqlite:///{tmp_path / 'prefect.db'}"),
        dataset_name="prefect_dataset",
        pipelines_dir=str(tmp_path / "pipelines"),
    )


def _run(tmp_path, source, table: str = TABLE) -> list[str]:
    pipeline = _pipeline(tmp_path)
    pipeline.run(source)
    query = f'SELECT id FROM "{table}" ORDER BY id'
    with pipeline.sql_client() as sql, sql.execute_query(query) as cursor:
        return [row[0] for row in cursor.fetchall()]


def test_state_and_deletes_survive_between_real_dlt_runs(tmp_path):
    fake = _workspace()
    first = prefect_source(service=fake, resource_name=TABLE)
    rows1 = _run(tmp_path, first)
    assert rows1 == ["deployment:d1", "flow:f1", "flow_run:r1"]
    assert first.cognee_sync_stats["failed"] == 0

    quiet = prefect_source(service=fake, resource_name=TABLE)
    rows2 = _run(tmp_path, quiet)
    assert rows2 == ["deployment:d1", "flow:f1", "flow_run:r1"]
    assert quiet.cognee_sync_stats["skipped"] == 3

    # Delete flow_run:r1 in fake
    del fake.flow_runs["r1"]
    forget = prefect_source(service=fake, resource_name=TABLE)
    rows3 = _run(tmp_path, forget)
    assert rows3 == ["deployment:d1", "flow:f1"]
    assert forget.cognee_sync_stats["deleted"] == 1
