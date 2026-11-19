"""The public factory, and the sync through a real dlt pipeline."""

import json

import pytest
from cognee.tasks.ingestion.dlt_utils import DOCUMENT_SOURCE_ATTR, PIPELINE_SCOPE_ATTR
from fake_ga4 import PROPERTY, FakeGA4

from cognee_community_connector_ga4 import ga4_source

BASE = f"properties/{PROPERTY}"
ALL_IDS = [
    f"{BASE}/audiences/41",
    f"{BASE}/customDimensions/11",
    f"{BASE}/customDimensions/12",
    f"{BASE}/keyEvents/31",
]


def _property() -> FakeGA4:
    fake = FakeGA4()
    fake.add_custom_dimension("11", "plan_tier", "Plan tier", scope="USER")
    fake.add_custom_dimension("12", "page_type", "Page type")
    fake.add_key_event("31", "purchase")
    fake.add_audience("41", "Purchasers", "Users who bought something.")
    return fake


def test_the_resource_is_a_merge_resource_tagged_for_document_ingestion():
    resource = ga4_source(PROPERTY, service=FakeGA4())

    assert resource.name == f"ga4_{PROPERTY}"
    assert getattr(resource, DOCUMENT_SOURCE_ATTR) == "ga4"
    assert getattr(resource, PIPELINE_SCOPE_ATTR) == f"ga4_{PROPERTY}"
    hints = resource.compute_table_schema()
    assert hints["write_disposition"] == "merge"
    assert hints["columns"]["id"]["primary_key"] is True
    assert hints["columns"]["_deleted"]["hard_delete"] is True
    assert resource.cognee_sync_stats == {}


@pytest.mark.parametrize("property_id", [int(PROPERTY), f"properties/{PROPERTY}", f" {PROPERTY} "])
def test_the_property_id_is_accepted_as_an_int_or_a_resource_name(property_id):
    assert ga4_source(property_id, service=FakeGA4()).name == f"ga4_{PROPERTY}"


@pytest.mark.parametrize(
    "kwargs",
    [
        {"property_id": ""},
        {"property_id": "G-ABC123XYZ"},
        {"property_id": "123/../456"},
        {"property_id": "١٢٣"},
        {"property_id": PROPERTY, "include": ()},
        {"property_id": PROPERTY, "include": ("audiences", "reports")},
    ],
)
def test_bad_arguments_are_rejected_when_the_resource_is_built(kwargs):
    with pytest.raises(ValueError):
        ga4_source(service=FakeGA4(), **kwargs)


def test_an_old_cognee_without_scoped_cleanup_is_refused(monkeypatch):
    from cognee.tasks.ingestion import dlt_utils

    monkeypatch.setattr(dlt_utils, "DOCUMENT_SYNC_VERSION", 0)

    with pytest.raises(RuntimeError, match="table-scoped document cleanup"):
        ga4_source(PROPERTY, service=FakeGA4())


def test_a_key_file_builds_the_resource_offline_without_showing_the_key(service_account_file):
    private_key = json.loads(service_account_file.read_text())["private_key"]
    key_body = private_key.splitlines()[1]

    resource = ga4_source(PROPERTY, credentials_path=str(service_account_file))

    assert resource.name == f"ga4_{PROPERTY}"
    assert key_body not in repr(resource)


def test_a_missing_key_file_fails_when_the_resource_is_built(tmp_path):
    with pytest.raises(FileNotFoundError):
        ga4_source(PROPERTY, credentials_path=str(tmp_path / "missing.json"))


def test_state_and_deletes_survive_between_real_dlt_runs(tmp_path):
    import dlt

    table = f"ga4_{PROPERTY}"

    def run(source) -> list[str]:
        pipeline = dlt.pipeline(
            pipeline_name="ga4_test",
            destination=dlt.destinations.sqlalchemy(f"sqlite:///{tmp_path / 'g.db'}"),
            dataset_name="ga4",
            pipelines_dir=str(tmp_path / "pipelines"),
        )
        pipeline.run(source)
        query = f'SELECT id FROM "{table}" ORDER BY id'
        with pipeline.sql_client() as sql, sql.execute_query(query) as cursor:
            return [row[0] for row in cursor.fetchall()]

    fake = _property()
    first = ga4_source(PROPERTY, service=fake)
    assert run(first) == ALL_IDS
    assert first.cognee_sync_stats["failed"] == 0

    quiet = ga4_source(PROPERTY, service=fake)
    run(quiet)
    assert quiet.cognee_sync_stats["skipped"] == quiet.cognee_sync_stats["scanned"] == 4

    del fake.records["custom_dimensions"]["12"]
    del fake.records["audiences"]["41"]
    forget = ga4_source(PROPERTY, service=fake)
    assert run(forget) == [f"{BASE}/customDimensions/11", f"{BASE}/keyEvents/31"]
    assert forget.cognee_sync_stats["deleted"] == 2
