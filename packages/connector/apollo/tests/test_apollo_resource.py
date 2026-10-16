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
        {"resource_name": TABLE},
        {"resource_name": "  ", "api_key": "key"},
        {"resource_name": TABLE, "api_key": "key", "include": ()},
        {"resource_name": TABLE, "api_key": "key", "include": ("contacts", "people")},
        {"resource_name": TABLE, "api_key": "bad key"},
    ],
)
def test_bad_arguments_are_rejected_when_the_resource_is_built(kwargs):
    with pytest.raises(ValueError):
        apollo_source(**kwargs)


def test_resource_name_has_no_default():
    with pytest.raises(TypeError, match="resource_name"):
        apollo_source(api_key="key")


def _pipeline(tmp_path):
    import dlt

    return dlt.pipeline(
        pipeline_name="apollo_test",
        destination=dlt.destinations.sqlalchemy(f"sqlite:///{tmp_path / 'a.db'}"),
        dataset_name="apollo",
        pipelines_dir=str(tmp_path / "pipelines"),
    )


def _run(tmp_path, source, table: str = TABLE) -> list[str]:
    pipeline = _pipeline(tmp_path)
    pipeline.run(source)
    query = f'SELECT id FROM "{table}" ORDER BY id'
    with pipeline.sql_client() as sql, sql.execute_query(query) as cursor:
        return [row[0] for row in cursor.fetchall()]


def test_state_and_deletes_survive_between_real_dlt_runs(tmp_path):
    def run(source) -> list[str]:
        return _run(tmp_path, source)

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


def test_a_reused_source_forgets_the_quota_its_client_saw_last_run(tmp_path):
    fake = _workspace()
    source = apollo_source(service=fake, resource_name=TABLE)
    _run(tmp_path, source)

    fake.rate_limit = {"minute": 0}  # stale: the window has reset since
    fake.add_contact("c3", "Cara Hopper")
    assert "contact:c3" in _run(tmp_path, source)
    assert source.cognee_sync_stats["failed"] == 0


def test_two_accounts_with_their_own_resource_names_stay_apart(tmp_path):
    team_a, team_b = _workspace(), FakeApollo(team_id="team2")
    team_b.add_contact("b1", "Dev Hollerith")
    team_b.add_contact("b2", "Eve Shannon")

    def run(fake, name):
        return _run(tmp_path, apollo_source(service=fake, resource_name=name), name)

    assert run(team_a, "apollo_a") == ["account:a1", "contact:c1", "contact:c2", "sequence:s1"]
    assert run(team_b, "apollo_b") == ["contact:b1", "contact:b2"]

    del team_b.contacts["b2"]
    assert run(team_b, "apollo_b") == ["contact:b1"]
    assert run(team_a, "apollo_a") == ["account:a1", "contact:c1", "contact:c2", "sequence:s1"]


def test_a_second_account_under_the_same_resource_name_is_refused(tmp_path):
    from dlt.pipeline.exceptions import PipelineStepFailed

    _run(tmp_path, apollo_source(service=_workspace(), resource_name=TABLE))

    other = FakeApollo(team_id="team2")
    other.add_contact("b1", "Dev Hollerith")
    with pytest.raises(PipelineStepFailed, match="resource_name"):
        _run(tmp_path, apollo_source(service=other, resource_name=TABLE))

    assert _run(tmp_path, apollo_source(service=_workspace(), resource_name=TABLE)) == [
        "account:a1",
        "contact:c1",
        "contact:c2",
        "sequence:s1",
    ]


def test_check_active_can_stop_a_run_through_the_resource(tmp_path):
    from dlt.pipeline.exceptions import PipelineStepFailed

    def revoked():
        raise PermissionError("access revoked")

    fake = _workspace()
    with pytest.raises(PipelineStepFailed, match="access revoked"):
        _run(tmp_path, apollo_source(service=fake, resource_name=TABLE, check_active=revoked))

    assert fake.calls == []
