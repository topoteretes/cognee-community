"""Tests for the checkly_source dlt resource and real dlt pipeline execution."""

import pytest
from cognee.tasks.ingestion.dlt_utils import DOCUMENT_SOURCE_ATTR, PIPELINE_SCOPE_ATTR
from fake_checkly import FakeCheckly

from cognee_community_connector_checkly import checkly_source

TABLE = "checkly_test"


def _workspace() -> FakeCheckly:
    fake = FakeCheckly()
    fake.add_check_group("g1", "Main Group")
    fake.add_check("c1", "API Health", groupId="g1")
    fake.add_alert_channel("a1", "PagerDuty Channel")
    return fake


def test_resource_merge_and_document_tagging():
    resource = checkly_source(service=FakeCheckly(), resource_name=TABLE)

    assert resource.name == TABLE
    assert getattr(resource, DOCUMENT_SOURCE_ATTR) == "checkly"
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
        {"resource_name": TABLE, "api_key": "bad key with spaces"},
        {"resource_name": TABLE, "api_key": "validkey", "include": ()},
        {"resource_name": TABLE, "api_key": "validkey", "include": ("checks", "invalid_kind")},
        {"resource_name": TABLE, "api_key": "validkey", "base_url": "invalid-url"},
    ],
)
def test_bad_arguments_rejected_at_construction(kwargs):
    with pytest.raises(ValueError):
        checkly_source(**kwargs)


def _pipeline(tmp_path):
    import dlt

    return dlt.pipeline(
        pipeline_name="checkly_test_pipeline",
        destination=dlt.destinations.sqlalchemy(f"sqlite:///{tmp_path / 'checkly.db'}"),
        dataset_name="checkly_dataset",
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
    first = checkly_source(service=fake, resource_name=TABLE)
    rows1 = _run(tmp_path, first)
    assert rows1 == ["alert_channel:a1", "check:c1", "check_group:g1"]
    assert first.cognee_sync_stats["failed"] == 0

    quiet = checkly_source(service=fake, resource_name=TABLE)
    rows2 = _run(tmp_path, quiet)
    assert rows2 == ["alert_channel:a1", "check:c1", "check_group:g1"]
    assert quiet.cognee_sync_stats["skipped"] == 3

    # Delete check:c1 in fake
    del fake.checks["c1"]
    forget = checkly_source(service=fake, resource_name=TABLE)
    rows3 = _run(tmp_path, forget)
    assert rows3 == ["alert_channel:a1", "check_group:g1"]
    assert forget.cognee_sync_stats["deleted"] == 1
