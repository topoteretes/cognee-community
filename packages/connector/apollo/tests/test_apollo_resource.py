"""The public factory, and the sync through a real dlt pipeline."""

import pytest
from cognee.tasks.ingestion.dlt_utils import DOCUMENT_SOURCE_ATTR, PIPELINE_SCOPE_ATTR
from fake_apollo import FakeApollo

from cognee_community_connector_apollo import apollo_source

TABLE = "apollo_test"


def _workspace() -> FakeApollo:
    fake = FakeApollo()
    fake.add_sequence("s1", "Q4 Outreach")
    fake.add_account("a1", "Acme")
    fake.add_contact("c1", "Ada Lovelace", account_id="a1")
    fake.add_contact("c2", "Ben Turing", account_id="a1")
    return fake


def test_the_resource_is_a_merge_resource_tagged_for_document_ingestion():
    resource = apollo_source(service=FakeApollo(), resource_name=TABLE)

    assert resource.name == TABLE
    assert getattr(resource, DOCUMENT_SOURCE_ATTR) == "apollo"
    assert getattr(resource, PIPELINE_SCOPE_ATTR) == TABLE
    hints = resource.compute_table_schema()
    assert hints["write_disposition"] == "merge"
    assert hints["columns"]["_deleted"]["hard_delete"] is True
    assert resource.cognee_sync_stats == {}


@pytest.mark.parametrize(
    "kwargs",
    [
        {},
        {"api_key": "key", "include": ()},
        {"api_key": "key", "include": ("contacts", "people")},
        {"api_key": "bad key"},
    ],
)
def test_bad_arguments_are_rejected_when_the_resource_is_built(kwargs):
    with pytest.raises(ValueError):
        apollo_source(**kwargs)


def test_state_and_deletes_survive_between_real_dlt_runs(tmp_path):
    import dlt

    def run(source) -> list[str]:
        pipeline = dlt.pipeline(
            pipeline_name="apollo_test",
            destination=dlt.destinations.sqlalchemy(f"sqlite:///{tmp_path / 'a.db'}"),
            dataset_name="apollo",
            pipelines_dir=str(tmp_path / "pipelines"),
        )
        pipeline.run(source)
        query = f'SELECT id FROM "{TABLE}" ORDER BY id'
        with pipeline.sql_client() as sql, sql.execute_query(query) as cursor:
            return [row[0] for row in cursor.fetchall()]

    fake = _workspace()
    first = apollo_source(service=fake, resource_name=TABLE)
    assert run(first) == ["account:a1", "contact:c1", "contact:c2", "sequence:s1"]
    assert first.cognee_sync_stats["failed"] == 0

    quiet = apollo_source(service=fake, resource_name=TABLE)
    run(quiet)
    assert quiet.cognee_sync_stats["skipped"] == quiet.cognee_sync_stats["scanned"] == 4

    del fake.contacts["c2"]
    forget = apollo_source(service=fake, resource_name=TABLE)
    assert run(forget) == ["account:a1", "contact:c1", "sequence:s1"]
    assert forget.cognee_sync_stats["deleted"] == 1
