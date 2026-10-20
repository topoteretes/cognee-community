"""The public factory, and the sync through a real dlt pipeline."""

import pytest
from cognee.tasks.ingestion.dlt_utils import DOCUMENT_SOURCE_ATTR, PIPELINE_SCOPE_ATTR
from fake_moodle import FakeMoodle

from cognee_community_connector_moodle import moodle_source

TABLE = "moodle_test"


def _course() -> FakeMoodle:
    fake = FakeMoodle()
    fake.add_course(7, "Algorithms")
    fake.add_page(7, "Sorting", "<p>Merge sort.</p>")
    fake.add_page(7, "Searching", "<p>Binary search.</p>")
    return fake


def _page_ids(fake: FakeMoodle) -> list[str]:
    return sorted(f"page:7:{cmid}" for cmid in fake.courses[7]["pages"])


def test_the_resource_is_a_merge_resource_tagged_for_document_ingestion():
    resource = moodle_source(service=FakeMoodle(), resource_name=TABLE)

    assert resource.name == TABLE
    assert getattr(resource, DOCUMENT_SOURCE_ATTR) == "moodle"
    assert getattr(resource, PIPELINE_SCOPE_ATTR) == TABLE
    hints = resource.compute_table_schema()
    assert hints["write_disposition"] == "merge"
    assert hints["columns"]["_deleted"]["hard_delete"] is True
    assert hints["columns"]["cognee_node_set"]["data_type"] == "json"
    assert resource.cognee_sync_stats == {}


@pytest.mark.parametrize(
    "kwargs",
    [
        {"resource_name": TABLE},  # no service, no credentials
        {"resource_name": "  ", "service": FakeMoodle()},
        {"resource_name": TABLE, "service": FakeMoodle(), "include": ()},
        {"resource_name": TABLE, "service": FakeMoodle(), "include": ("pages", "grades")},
        {"resource_name": TABLE, "base_url": "https://m.example.edu", "token": "bad key"},
        {"resource_name": TABLE, "base_url": "http://m.example.edu", "token": "key"},
    ],
)
def test_bad_arguments_are_rejected_when_the_resource_is_built(kwargs, monkeypatch):
    monkeypatch.delenv("MOODLE_BASE_URL", raising=False)
    monkeypatch.delenv("MOODLE_TOKEN", raising=False)
    with pytest.raises(ValueError):
        moodle_source(**kwargs)


def test_resource_name_has_no_default():
    with pytest.raises(TypeError, match="resource_name"):
        moodle_source(service=FakeMoodle())


def test_credentials_fall_back_to_the_environment(monkeypatch):
    monkeypatch.setenv("MOODLE_BASE_URL", "https://moodle.example.edu/")
    monkeypatch.setenv("MOODLE_TOKEN", "envtoken123")

    assert moodle_source(resource_name=TABLE).name == TABLE


def _pipeline(tmp_path):
    import dlt

    return dlt.pipeline(
        pipeline_name="moodle_test",
        destination=dlt.destinations.sqlalchemy(f"sqlite:///{tmp_path / 'm.db'}"),
        dataset_name="moodle",
        pipelines_dir=str(tmp_path / "pipelines"),
    )


def _run(tmp_path, source, table: str = TABLE) -> list[str]:
    pipeline = _pipeline(tmp_path)
    pipeline.run(source)
    query = f'SELECT id FROM "{table}" ORDER BY id'
    with pipeline.sql_client() as sql, sql.execute_query(query) as cursor:
        return [row[0] for row in cursor.fetchall()]


def test_state_and_deletes_survive_between_real_dlt_runs(tmp_path):
    fake = _course()
    section = f"section:7:{fake.courses[7]['sections'][0]['id']}"
    expected = sorted(["course:7", section, *_page_ids(fake)])

    first = moodle_source(service=fake, resource_name=TABLE, include=("pages",))
    assert _run(tmp_path, first) == expected
    assert first.cognee_sync_stats["failed"] == 0

    quiet = moodle_source(service=fake, resource_name=TABLE, include=("pages",))
    _run(tmp_path, quiet)
    assert quiet.cognee_sync_stats["skipped"] == quiet.cognee_sync_stats["scanned"] == 4

    gone = _page_ids(fake)[1]
    fake.remove(7, int(gone.rsplit(":", 1)[1]))
    forget = moodle_source(service=fake, resource_name=TABLE, include=("pages",))
    # the section re-renders (it lost a module) and the page leaves
    assert _run(tmp_path, forget) == [row for row in expected if row != gone]
    assert forget.cognee_sync_stats["deleted"] == 1


def test_the_node_set_column_is_stored_as_a_json_list(tmp_path):
    import json

    fake = _course()
    pipeline = _pipeline(tmp_path)
    pipeline.run(moodle_source(service=fake, resource_name=TABLE, include=("pages",)))

    with (
        pipeline.sql_client() as sql,
        sql.execute_query(f"SELECT cognee_node_set FROM \"{TABLE}\" WHERE id = 'course:7'") as c,
    ):
        (value,) = c.fetchall()[0]
    assert (json.loads(value) if isinstance(value, str) else value) == ["course:7"]


def test_two_sites_with_their_own_resource_names_stay_apart(tmp_path):
    site_a, site_b = _course(), FakeMoodle()
    site_b.add_course(50, "Biology")
    site_b.add_page(50, "Cells", "<p>Membranes.</p>")

    def run(fake, name):
        return _run(
            tmp_path, moodle_source(service=fake, resource_name=name, include=("pages",)), name
        )

    a_rows = run(site_a, "moodle_a")
    b_rows = run(site_b, "moodle_b")
    assert {r.split(":")[1] for r in a_rows} == {"7"}
    assert {r.split(":")[1] for r in b_rows} == {"50"}

    site_b.remove(50, next(iter(site_b.courses[50]["pages"])))
    assert len(run(site_b, "moodle_b")) == len(b_rows) - 1
    assert run(site_a, "moodle_a") == a_rows


def test_check_active_can_stop_a_run_through_the_resource(tmp_path):
    from dlt.pipeline.exceptions import PipelineStepFailed

    def revoked():
        raise PermissionError("access revoked")

    fake = _course()
    with pytest.raises(PipelineStepFailed, match="access revoked"):
        _run(tmp_path, moodle_source(service=fake, resource_name=TABLE, check_active=revoked))

    assert fake.calls == []
