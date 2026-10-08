"""The public factory, and the sync through a real dlt pipeline."""

import pytest
from cognee.tasks.ingestion.dlt_utils import DOCUMENT_SOURCE_ATTR, PIPELINE_SCOPE_ATTR
from fake_amplitude import FakeAmplitude

from cognee_community_connector_amplitude import amplitude_source

TABLE = "amplitude_test"
ALL_IDS = ["annotation:1", "cohort:c1", "cohort:c2", "event:Checkout Completed"]


def _project() -> FakeAmplitude:
    fake = FakeAmplitude()
    fake.add_event("Checkout Completed", description="A customer paid for an order.")
    fake.add_event_property("Checkout Completed", "cart_value", type="number")
    fake.add_cohort("c1", "Paying users")
    fake.add_cohort("c2", "Power users")
    fake.add_annotation(1, "Checkout redesign shipped")
    return fake


def test_the_resource_is_a_merge_resource_tagged_for_document_ingestion():
    resource = amplitude_source(service=FakeAmplitude(), resource_name=TABLE)

    assert resource.name == TABLE
    assert getattr(resource, DOCUMENT_SOURCE_ATTR) == "amplitude"
    assert getattr(resource, PIPELINE_SCOPE_ATTR) == TABLE
    hints = resource.compute_table_schema()
    assert hints["write_disposition"] == "merge"
    assert hints["columns"]["id"]["primary_key"] is True
    assert hints["columns"]["_deleted"]["hard_delete"] is True
    assert resource.cognee_sync_stats == {}


@pytest.mark.parametrize(
    "kwargs",
    [
        {},
        {"api_key": "key"},
        {"secret_key": "secret"},
        {"api_key": "key", "secret_key": "secret", "include": ()},
        {"api_key": "key", "secret_key": "secret", "include": ("events", "charts")},
        {"api_key": "bad key", "secret_key": "secret"},
        {"api_key": "key", "secret_key": "bad secret"},
        {"api_key": "key", "secret_key": "secret", "region": "apac"},
        {"api_key": "key", "secret_key": "secret", "chart_ids": ["abc/../def"]},
        {"api_key": "key", "secret_key": "secret", "chart_ids": [""]},
        {"api_key": "key", "secret_key": "secret", "chart_ids": [123]},
    ],
)
def test_bad_arguments_are_rejected_when_the_resource_is_built(kwargs):
    with pytest.raises(ValueError):
        amplitude_source(**kwargs)


def test_an_old_cognee_without_scoped_cleanup_is_refused(monkeypatch):
    from cognee.tasks.ingestion import dlt_utils

    monkeypatch.setattr(dlt_utils, "DOCUMENT_SYNC_VERSION", 0)

    with pytest.raises(RuntimeError, match="table-scoped document cleanup"):
        amplitude_source(service=FakeAmplitude())


def test_state_and_deletes_survive_between_real_dlt_runs(tmp_path):
    import dlt

    def run(source) -> list[str]:
        pipeline = dlt.pipeline(
            pipeline_name="amplitude_test",
            destination=dlt.destinations.sqlalchemy(f"sqlite:///{tmp_path / 'a.db'}"),
            dataset_name="amplitude",
            pipelines_dir=str(tmp_path / "pipelines"),
        )
        pipeline.run(source)
        query = f'SELECT id FROM "{TABLE}" ORDER BY id'
        with pipeline.sql_client() as sql, sql.execute_query(query) as cursor:
            return [row[0] for row in cursor.fetchall()]

    fake = _project()
    first = amplitude_source(service=fake, resource_name=TABLE)
    assert run(first) == ALL_IDS
    assert first.cognee_sync_stats["failed"] == 0

    quiet = amplitude_source(service=fake, resource_name=TABLE)
    run(quiet)
    assert quiet.cognee_sync_stats["skipped"] == quiet.cognee_sync_stats["scanned"] == 4

    del fake.cohorts["c2"]
    del fake.annotations[1]
    forget = amplitude_source(service=fake, resource_name=TABLE)
    assert run(forget) == ["cohort:c1", "event:Checkout Completed"]
    assert forget.cognee_sync_stats["deleted"] == 2
