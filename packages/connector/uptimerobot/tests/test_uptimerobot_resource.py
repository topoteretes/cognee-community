"""Tests for the uptimerobot_source dlt resource and real dlt pipeline execution."""

import pytest
from cognee.tasks.ingestion.dlt_utils import DOCUMENT_SOURCE_ATTR, PIPELINE_SCOPE_ATTR
from fake_uptimerobot import FakeUptimeRobot

from cognee_community_connector_uptimerobot import uptimerobot_source

TABLE = "uptimerobot_test"


def _workspace() -> FakeUptimeRobot:
    fake = FakeUptimeRobot()
    fake.add_monitor("101", "API Monitor")
    fake.add_psp("201", "Status Page", monitors=[101])
    fake.add_alert_contact("301", "DevOps Slack")
    return fake


def test_resource_merge_and_document_tagging():
    resource = uptimerobot_source(service=FakeUptimeRobot(), resource_name=TABLE)

    assert resource.name == TABLE
    assert getattr(resource, DOCUMENT_SOURCE_ATTR) == "uptimerobot"
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
        {"resource_name": TABLE, "api_key": "validkey", "include": ("monitors", "invalid_kind")},
        {"resource_name": TABLE, "api_key": "validkey", "base_url": "invalid-url"},
    ],
)
def test_bad_arguments_rejected_at_construction(kwargs):
    with pytest.raises(ValueError):
        uptimerobot_source(**kwargs)


def _pipeline(tmp_path):
    import dlt

    return dlt.pipeline(
        pipeline_name="uptimerobot_test_pipeline",
        destination=dlt.destinations.sqlalchemy(f"sqlite:///{tmp_path / 'uptimerobot.db'}"),
        dataset_name="uptimerobot_dataset",
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
    first = uptimerobot_source(service=fake, resource_name=TABLE)
    rows1 = _run(tmp_path, first)
    assert rows1 == ["alert_contact:301", "monitor:101", "psp:201"]
    assert first.cognee_sync_stats["failed"] == 0

    quiet = uptimerobot_source(service=fake, resource_name=TABLE)
    rows2 = _run(tmp_path, quiet)
    assert rows2 == ["alert_contact:301", "monitor:101", "psp:201"]
    assert quiet.cognee_sync_stats["skipped"] == 3

    # Delete monitor:101 in fake
    del fake.monitors["101"]
    forget = uptimerobot_source(service=fake, resource_name=TABLE)
    rows3 = _run(tmp_path, forget)
    assert rows3 == ["alert_contact:301", "psp:201"]
    assert forget.cognee_sync_stats["deleted"] == 1
