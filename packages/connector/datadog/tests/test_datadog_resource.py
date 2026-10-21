"""The public factory, and the sync through a real dlt pipeline."""

import pytest
from cognee.tasks.ingestion.dlt_utils import DOCUMENT_SOURCE_ATTR, PIPELINE_SCOPE_ATTR
from fake_datadog import API_KEY, APP_KEY, FakeDatadog, client_for

from cognee_community_connector_datadog import datadog_source

TABLE = "datadog_test"


def _org() -> FakeDatadog:
    fake = FakeDatadog()
    fake.add_monitor(1, "Checkout latency", "Runbook: roll back payments-api.")
    fake.add_dashboard("abc-123", "Checkout")
    return fake


def test_the_resource_is_a_merge_resource_tagged_for_document_ingestion():
    resource = datadog_source(service=client_for(FakeDatadog()), resource_name=TABLE)

    assert resource.name == TABLE
    assert getattr(resource, DOCUMENT_SOURCE_ATTR) == "datadog"
    assert getattr(resource, PIPELINE_SCOPE_ATTR) == TABLE
    hints = resource.compute_table_schema()
    assert hints["write_disposition"] == "merge"
    assert hints["columns"]["_deleted"]["hard_delete"] is True
    assert resource.cognee_sync_stats == {}


@pytest.mark.parametrize(
    "kwargs",
    [
        {"resource_name": TABLE},
        {"resource_name": TABLE, "api_key": API_KEY},
        {"resource_name": "  ", "api_key": API_KEY, "app_key": APP_KEY},
        {"resource_name": TABLE, "api_key": API_KEY, "app_key": APP_KEY, "include": ()},
        {
            "resource_name": TABLE,
            "api_key": API_KEY,
            "app_key": APP_KEY,
            "include": ("monitors", "logs"),
        },
        {"resource_name": TABLE, "api_key": API_KEY, "app_key": APP_KEY, "site": "eu"},
        {"resource_name": TABLE, "api_key": "bad key", "app_key": APP_KEY},
    ],
)
def test_bad_arguments_are_rejected_when_the_resource_is_built(kwargs):
    with pytest.raises(ValueError):
        datadog_source(**kwargs)


def test_a_cognee_without_table_scoped_cleanup_is_refused(monkeypatch):
    from cognee.tasks.ingestion import dlt_utils

    monkeypatch.setattr(dlt_utils, "DOCUMENT_SYNC_VERSION", 0)

    with pytest.raises(RuntimeError, match="table-scoped"):
        datadog_source(service=client_for(FakeDatadog()), resource_name=TABLE)


def test_resource_name_has_no_default():
    with pytest.raises(TypeError, match="resource_name"):
        datadog_source(api_key=API_KEY, app_key=APP_KEY)


def _pipeline(tmp_path):
    import dlt

    return dlt.pipeline(
        pipeline_name="datadog_test",
        destination=dlt.destinations.sqlalchemy(f"sqlite:///{tmp_path / 'd.db'}"),
        dataset_name="datadog",
        pipelines_dir=str(tmp_path / "pipelines"),
    )


def _ids(pipeline) -> set[str]:
    with pipeline.sql_client() as client:
        return {row[0] for row in client.execute_sql(f"SELECT id FROM {TABLE}")}


def test_a_dlt_pipeline_keeps_state_between_runs_and_hard_deletes_tombstones(tmp_path):
    fake = _org()
    pipeline = _pipeline(tmp_path)

    first = datadog_source(service=client_for(fake), resource_name=TABLE)
    pipeline.run(first)
    assert _ids(pipeline) == {"monitor:1", "dashboard:abc-123"}
    assert first.cognee_sync_stats["scanned"] == 2

    quiet = datadog_source(service=client_for(fake), resource_name=TABLE)
    pipeline.run(quiet)
    assert quiet.cognee_sync_stats["skipped"] == 2, "the state survived between runs"

    del fake.monitors["1"]
    pipeline.run(datadog_source(service=client_for(fake), resource_name=TABLE))
    assert _ids(pipeline) == {"dashboard:abc-123"}


def test_check_active_can_stop_a_sync():
    class RevokedError(Exception):
        pass

    def check_active():
        raise RevokedError()

    resource = datadog_source(
        service=client_for(_org()), resource_name=TABLE, check_active=check_active
    )
    with pytest.raises(Exception) as error:
        list(resource)
    assert isinstance(error.value, RevokedError) or isinstance(error.value.__cause__, RevokedError)
