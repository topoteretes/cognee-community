"""Tests for the unkey_source dlt resource and real dlt pipeline execution."""

import pytest
from cognee.tasks.ingestion.dlt_utils import DOCUMENT_SOURCE_ATTR, PIPELINE_SCOPE_ATTR
from fake_unkey import FakeUnkey

from cognee_community_connector_unkey import unkey_source

TABLE = "unkey_test"


def _workspace() -> FakeUnkey:
    fake = FakeUnkey()
    fake.add_api("api_1", "Production Gateway")
    fake.add_key("api_1", "key_1", "Main Key")
    fake.add_identity("id_1", externalId="cust_acme")
    return fake


def test_resource_merge_and_document_tagging():
    resource = unkey_source(service=FakeUnkey(), resource_name=TABLE)

    assert resource.name == TABLE
    assert getattr(resource, DOCUMENT_SOURCE_ATTR) == "unkey"
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
        {"resource_name": TABLE, "root_key": "bad key with spaces"},
        {"resource_name": TABLE, "root_key": "validkey", "include": ()},
        {"resource_name": TABLE, "root_key": "validkey", "include": ("apis", "bad_kind")},
        {"resource_name": TABLE, "root_key": "validkey", "base_url": "invalid-url"},
    ],
)
def test_bad_arguments_rejected_at_construction(kwargs):
    with pytest.raises(ValueError):
        unkey_source(**kwargs)


def _pipeline(tmp_path):
    import dlt

    return dlt.pipeline(
        pipeline_name="unkey_test_pipeline",
        destination=dlt.destinations.sqlalchemy(f"sqlite:///{tmp_path / 'unkey.db'}"),
        dataset_name="unkey_dataset",
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
    first = unkey_source(service=fake, resource_name=TABLE)
    rows1 = _run(tmp_path, first)
    assert rows1 == ["api:api_1", "identity:id_1", "key:api_1:key_1"]
    assert first.cognee_sync_stats["failed"] == 0

    quiet = unkey_source(service=fake, resource_name=TABLE)
    rows2 = _run(tmp_path, quiet)
    assert rows2 == ["api:api_1", "identity:id_1", "key:api_1:key_1"]
    assert quiet.cognee_sync_stats["skipped"] == 3

    # Delete key_1
    del fake.keys["api_1"]["key_1"]
    forget = unkey_source(service=fake, resource_name=TABLE)
    rows3 = _run(tmp_path, forget)
    assert rows3 == ["api:api_1", "identity:id_1"]
    assert forget.cognee_sync_stats["deleted"] == 1
