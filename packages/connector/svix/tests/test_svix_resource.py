"""Tests for the svix_source dlt resource and real dlt pipeline execution."""

import pytest
from cognee.tasks.ingestion.dlt_utils import DOCUMENT_SOURCE_ATTR, PIPELINE_SCOPE_ATTR
from fake_svix import FakeSvix

from cognee_community_connector_svix import svix_source

TABLE = "svix_test"


def _workspace() -> FakeSvix:
    fake = FakeSvix()
    fake.add_app("app_1", "Production SaaS")
    fake.add_endpoint("app_1", "ep_1", "https://api.example.com/hook")
    fake.add_event_type("invoice.paid")
    return fake


def test_resource_merge_and_document_tagging():
    resource = svix_source(service=FakeSvix(), resource_name=TABLE)

    assert resource.name == TABLE
    assert getattr(resource, DOCUMENT_SOURCE_ATTR) == "svix"
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
        {"resource_name": TABLE, "api_token": "bad token with spaces"},
        {"resource_name": TABLE, "api_token": "validtoken", "include": ()},
        {
            "resource_name": TABLE,
            "api_token": "validtoken",
            "include": ("applications", "bad_kind"),
        },
        {"resource_name": TABLE, "api_token": "validtoken", "base_url": "invalid-url"},
    ],
)
def test_bad_arguments_rejected_at_construction(kwargs):
    with pytest.raises(ValueError):
        svix_source(**kwargs)


def _pipeline(tmp_path):
    import dlt

    return dlt.pipeline(
        pipeline_name="svix_test_pipeline",
        destination=dlt.destinations.sqlalchemy(f"sqlite:///{tmp_path / 'svix.db'}"),
        dataset_name="svix_dataset",
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
    first = svix_source(service=fake, resource_name=TABLE)
    rows1 = _run(tmp_path, first)
    assert rows1 == ["app:app_1", "endpoint:app_1:ep_1", "event_type:invoice.paid"]
    assert first.cognee_sync_stats["failed"] == 0

    quiet = svix_source(service=fake, resource_name=TABLE)
    rows2 = _run(tmp_path, quiet)
    assert rows2 == ["app:app_1", "endpoint:app_1:ep_1", "event_type:invoice.paid"]
    assert quiet.cognee_sync_stats["skipped"] == 3

    # Delete endpoint ep_1
    del fake.endpoints["app_1"]["ep_1"]
    forget = svix_source(service=fake, resource_name=TABLE)
    rows3 = _run(tmp_path, forget)
    assert rows3 == ["app:app_1", "event_type:invoice.paid"]
    assert forget.cognee_sync_stats["deleted"] == 1
