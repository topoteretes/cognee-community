"""The public factory, and the sync through a real dlt pipeline."""

import pytest
from cognee.tasks.ingestion.dlt_utils import DOCUMENT_SOURCE_ATTR, PIPELINE_SCOPE_ATTR
from fake_gramps import BASE_URL, FakeGramps, small_tree
from helpers import client_for

from cognee_community_connector_gramps import gramps_source

TABLE = "gramps_test"


def _source(fake, **kwargs):
    kwargs.setdefault("resource_name", TABLE)
    return gramps_source(client=client_for(fake), **kwargs)


def test_the_resource_is_a_merge_resource_tagged_for_document_ingestion():
    resource = _source(FakeGramps())

    assert resource.name == TABLE
    assert getattr(resource, DOCUMENT_SOURCE_ATTR) == "gramps"
    assert getattr(resource, PIPELINE_SCOPE_ATTR) == TABLE
    hints = resource.compute_table_schema()
    assert hints["write_disposition"] == "merge"
    assert hints["columns"]["_deleted"]["hard_delete"] is True
    assert resource.cognee_sync_stats == {}


def test_the_default_resource_name_is_stable_per_server_and_user(monkeypatch):
    monkeypatch.setenv("GRAMPS_URL", BASE_URL)
    monkeypatch.setenv("GRAMPS_USERNAME", "owner")
    monkeypatch.setenv("GRAMPS_PASSWORD", "pw")
    first, second = gramps_source(), gramps_source(base_url=BASE_URL + "/api/")
    assert first.name == second.name
    assert first.name.startswith("gramps_") and len(first.name) == len("gramps_") + 12
    assert gramps_source(username="someone").name != first.name


@pytest.mark.parametrize(
    "kwargs",
    [
        {"base_url": "gramps.test", "username": "u", "password": "p"},
        {"base_url": BASE_URL, "username": "", "password": "p"},
        {"base_url": BASE_URL, "username": "u", "password": ""},
        {"base_url": BASE_URL, "username": "u", "password": "p", "include": ()},
        {"base_url": BASE_URL, "username": "u", "password": "p", "include": ("events",)},
        {"base_url": BASE_URL, "username": "u", "password": "p", "tags": "Family"},
        {"base_url": BASE_URL, "username": "u", "password": "p", "tags": []},
        {"base_url": BASE_URL, "username": "u", "password": "p", "full_sync_hours": 0},
        {"base_url": BASE_URL, "username": "u", "password": "p", "resource_name": " "},
    ],
)
def test_bad_arguments_are_rejected_when_the_resource_is_built(kwargs, monkeypatch):
    for name in ("GRAMPS_URL", "GRAMPS_USERNAME", "GRAMPS_PASSWORD"):
        monkeypatch.delenv(name, raising=False)
    with pytest.raises(ValueError):
        gramps_source(**kwargs)


def _pipeline(tmp_path):
    import dlt

    return dlt.pipeline(
        pipeline_name="gramps_test",
        destination=dlt.destinations.sqlalchemy(f"sqlite:///{tmp_path / 'g.db'}"),
        dataset_name="gramps",
        pipelines_dir=str(tmp_path / "pipelines"),
    )


def _run(tmp_path, source, table: str = TABLE) -> list[str]:
    pipeline = _pipeline(tmp_path)
    pipeline.run(source)
    query = f'SELECT id FROM "{table}" ORDER BY id'
    with pipeline.sql_client() as sql, sql.execute_query(query) as cursor:
        return [row[0] for row in cursor.fetchall()]


ALL = [
    "gramps:family:f1",
    "gramps:person:p_anna",
    "gramps:person:p_john",
    "gramps:person:p_mary",
]


def test_state_and_deletes_survive_between_real_dlt_runs(tmp_path):
    fake = small_tree()
    first = _source(fake)
    assert _run(tmp_path, first) == ALL
    assert first.cognee_sync_stats["synced"] == 4

    quiet = _source(fake)
    assert _run(tmp_path, quiet) == ALL
    assert quiet.cognee_sync_stats["full"] == 0

    fake.edit("families", "f1", mother_handle=None)
    fake.delete("people", "p_anna")
    forget = _source(fake)
    assert _run(tmp_path, forget) == [i for i in ALL if i != "gramps:person:p_anna"]
    assert forget.cognee_sync_stats["deleted"] == 1


def test_a_second_tree_under_the_same_resource_name_is_refused(tmp_path):
    from dlt.pipeline.exceptions import PipelineStepFailed

    _run(tmp_path, _source(small_tree()))
    other = small_tree(FakeGramps(tree_id="other999"))
    with pytest.raises(PipelineStepFailed, match="resource_name"):
        _run(tmp_path, _source(other))
    assert _run(tmp_path, _source(small_tree())) == ALL


def test_check_active_can_stop_a_run_through_the_resource(tmp_path):
    from dlt.pipeline.exceptions import PipelineStepFailed

    def revoked():
        raise PermissionError("access revoked")

    fake = small_tree()
    with pytest.raises(PipelineStepFailed, match="access revoked"):
        _run(tmp_path, _source(fake, check_active=revoked))
    assert fake.calls == []
