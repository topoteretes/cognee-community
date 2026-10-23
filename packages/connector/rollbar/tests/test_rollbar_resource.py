"""The public factory, and the sync through a real dlt pipeline."""

import pytest
from cognee.tasks.ingestion.dlt_utils import DOCUMENT_SOURCE_ATTR, PIPELINE_SCOPE_ATTR
from dlt.extract.exceptions import ResourceExtractionError
from fake_rollbar import PROJECT, TOKEN, FakeRollbar

from cognee_community_connector_rollbar import rollbar_source


def _source(fake: FakeRollbar, **kwargs):
    return rollbar_source(PROJECT, access_token=TOKEN, http_client=fake.client(), **kwargs)


def test_the_resource_is_a_merge_resource_tagged_for_document_ingestion():
    resource = _source(FakeRollbar())

    assert resource.name == f"rollbar_{PROJECT}"
    assert getattr(resource, DOCUMENT_SOURCE_ATTR) == "rollbar"
    assert getattr(resource, PIPELINE_SCOPE_ATTR) == f"rollbar_{PROJECT}"
    hints = resource.compute_table_schema()
    assert hints["write_disposition"] == "merge"
    assert hints["columns"]["id"]["primary_key"] is True
    assert hints["columns"]["_deleted"]["hard_delete"] is True
    assert resource.cognee_sync_stats == {}


@pytest.mark.parametrize("project_id", [PROJECT, str(PROJECT), f" {PROJECT} "])
def test_the_project_id_is_accepted_as_an_int_or_a_string(project_id):
    resource = rollbar_source(project_id, access_token=TOKEN)

    assert resource.name == f"rollbar_{PROJECT}"


@pytest.mark.parametrize(
    "kwargs",
    [
        {"project_id": ""},
        {"project_id": "FirstProject"},
        {"project_id": "123/../456"},
        {"project_id": "١٢٣"},
        {"project_id": PROJECT, "statuses": ()},
        {"project_id": PROJECT, "statuses": ("active", "deleted")},
        {"project_id": PROJECT, "levels": ("fatal",)},
        {"project_id": PROJECT, "environments": ("",)},
    ],
)
def test_bad_arguments_are_rejected_when_the_resource_is_built(kwargs):
    with pytest.raises(ValueError):
        rollbar_source(access_token=TOKEN, **kwargs)


def test_a_single_string_is_one_choice_not_a_list_of_letters():
    fake = FakeRollbar()
    list(_source(fake, statuses="muted", environments="production"))

    assert fake.requests[0].url.params.get_list("status") == ["muted"]
    assert fake.requests[0].url.params.get_list("environment") == ["production"]


def test_a_token_is_required(monkeypatch):
    monkeypatch.delenv("ROLLBAR_ACCESS_TOKEN", raising=False)

    with pytest.raises(ValueError, match="ROLLBAR_ACCESS_TOKEN"):
        rollbar_source(PROJECT)


def test_the_token_can_come_from_the_environment_and_is_never_shown(monkeypatch):
    fake = FakeRollbar()
    fake.add_item("KeyError: 'plan_tier'")
    monkeypatch.setenv("ROLLBAR_ACCESS_TOKEN", TOKEN)

    resource = rollbar_source(PROJECT, http_client=fake.client())
    rows = list(resource)

    assert len(rows) == 1
    assert TOKEN not in repr(resource)
    assert TOKEN not in repr(rows)


def test_a_cognee_without_per_row_node_sets_is_refused(monkeypatch):
    from cognee.tasks.ingestion import dlt_utils

    monkeypatch.setattr(dlt_utils, "DOCUMENT_SYNC_VERSION", 1)

    with pytest.raises(RuntimeError, match="DOCUMENT_SYNC_VERSION"):
        rollbar_source(PROJECT, access_token=TOKEN)


def test_check_active_runs_between_rows_and_can_stop_the_sync():
    fake = FakeRollbar()
    for number in range(3):
        fake.add_item(f"Error {number}")
    calls = []

    def check_active():
        calls.append(1)
        if len(calls) > 3:
            raise PermissionError("connection revoked")

    # dlt wraps whatever the check raises
    with pytest.raises(ResourceExtractionError, match="connection revoked"):
        list(_source(fake, check_active=check_active))
    assert len(calls) == 4


def test_state_and_deletes_survive_between_real_dlt_runs(tmp_path):
    import dlt

    table = f"rollbar_{PROJECT}"

    def run(source) -> list[str]:
        pipeline = dlt.pipeline(
            pipeline_name="rollbar_test",
            destination=dlt.destinations.sqlalchemy(f"sqlite:///{tmp_path / 'r.db'}"),
            dataset_name="rollbar",
            pipelines_dir=str(tmp_path / "pipelines"),
        )
        pipeline.run(source)
        query = f'SELECT title FROM "{table}" ORDER BY title'
        with pipeline.sql_client() as sql, sql.execute_query(query) as cursor:
            return [row[0] for row in cursor.fetchall()]

    fake = FakeRollbar()
    kept = fake.add_item("KeyError: 'plan_tier'")
    gone = fake.add_item("ValueError: invalid coupon code")

    first = _source(fake)
    assert run(first) == ["KeyError: 'plan_tier'", "ValueError: invalid coupon code"]
    assert first.cognee_sync_stats["failed"] == 0

    quiet = _source(fake)
    run(quiet)
    assert quiet.cognee_sync_stats["skipped"] == quiet.cognee_sync_stats["scanned"] == 2

    fake.delete_item_occurrences(gone)
    fake.resolve(kept)
    forget = _source(fake)
    assert run(forget) == ["KeyError: 'plan_tier'"]
    assert forget.cognee_sync_stats["deleted"] == 1
    assert forget.cognee_sync_stats["skipped"] == 0
