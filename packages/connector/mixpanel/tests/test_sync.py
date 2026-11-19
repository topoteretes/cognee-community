import asyncio
import copy
import json

import dlt
import pytest
from conftest import FakeClient

from cognee_community_connector_mixpanel import mixpanel_source, sync_mixpanel
from cognee_community_connector_mixpanel.mixpanel import MIXPANEL_TABLE_NAME, _sync_lock


def pipeline_at(tmp_path, name="synthetic"):
    return dlt.pipeline(
        pipeline_name=name,
        pipelines_dir=str(tmp_path / "pipelines"),
        destination=dlt.destinations.sqlalchemy(f"sqlite:///{tmp_path / 'stage.db'}"),
        dataset_name="definitions",
    )


def run(pipeline, client, **kwargs):
    info = pipeline.run(
        mixpanel_source(client=client, **kwargs), write_disposition="merge", primary_key="id"
    )
    with pipeline.sql_client() as sql:
        rows = sql.execute_sql(f"SELECT id, title, content FROM {MIXPANEL_TABLE_NAME} ORDER BY id")
    return rows, info


def test_baseline_change_unchanged_final_delete_restore(tmp_path, report, cohort, schema):
    client = FakeClient(tmp_path, report, cohort, schema)
    pipeline = pipeline_at(tmp_path)
    first, _ = run(pipeline, client)
    assert len(first) == 3
    same, info = run(pipeline, client)
    assert same == first
    table_jobs = [
        job
        for pack in info.load_packages
        for job in pack.jobs["completed_jobs"]
        if job.job_file_info.table_name == MIXPANEL_TABLE_NAME
    ]
    assert table_jobs == []
    client.data["report"][0]["name"] = "Renamed"
    changed, _ = run(pipeline, client)
    assert len(changed) == 3 and changed != first
    client.data = {"report": [], "cohort": [], "schema": []}
    assert run(pipeline, client)[0] == []
    client.data["report"] = [report]
    assert len(run(pipeline, client)[0]) == 1


def test_partial_read_preserves_staging_and_checkpoint(tmp_path, report, cohort):
    client = FakeClient(tmp_path, report, cohort)
    pipeline = pipeline_at(tmp_path)
    first, _ = run(pipeline, client)
    old_state = json.dumps(pipeline.state["sources"], sort_keys=True)
    client.fail_kind = "report"
    with pytest.raises(Exception, match="partial read"):
        run(pipeline, client)
    assert json.dumps(pipeline.state["sources"], sort_keys=True) == old_state
    with pipeline.sql_client() as sql:
        assert (
            sql.execute_sql(f"SELECT id, title, content FROM {MIXPANEL_TABLE_NAME} ORDER BY id")
            == first
        )


def test_disable_resource_reconciles_prior_inventory(tmp_path, report, cohort):
    client = FakeClient(tmp_path, report, cohort)
    pipeline = pipeline_at(tmp_path)
    assert len(run(pipeline, client)[0]) == 2
    remaining, _ = run(pipeline, client, resources=("report",))
    assert len(remaining) == 1 and "report" in remaining[0][0]


def test_selection_and_limit_before_yield(tmp_path, report, cohort):
    client = FakeClient(tmp_path, report, cohort)
    source = mixpanel_source(client=client, report_ids=[])
    rows = list(source)
    assert len(rows) == 1 and "Cohort" in rows[0]["title"]
    with pytest.raises(Exception, match="max_items"):
        list(mixpanel_source(client=client, max_items=1))


def test_changing_inventory_and_access_are_not_deletions(tmp_path, report):
    client = FakeClient(tmp_path, report)
    client.inventory_hook = lambda kind, n: copy.deepcopy(client.data[kind]) if n < 4 else []
    with pytest.raises(Exception, match="Inventory changed"):
        list(mixpanel_source(client=client))
    client.inventory_hook = None
    pipeline = pipeline_at(tmp_path)
    run(pipeline, client)
    client.access = "restricted"
    client.data["report"] = []
    with pytest.raises(Exception, match="access changed"):
        run(pipeline, client)


@pytest.mark.asyncio
async def test_helper_sets_merge_dataset_and_waits_for_cognify(tmp_path, report, monkeypatch):
    import cognee

    calls = []

    async def add(source, **kwargs):
        calls.append(("add", kwargs))

    async def cognify(**kwargs):
        calls.append(("cognify", kwargs))

    monkeypatch.setattr(cognee, "add", add)
    monkeypatch.setattr(cognee, "cognify", cognify)
    source = mixpanel_source(client=FakeClient(tmp_path, report))
    dataset = await sync_mixpanel(source)
    assert calls[0][1]["write_disposition"] == "merge"
    assert calls[0][1]["dataset_name"] == dataset
    assert calls[1] == ("cognify", {"datasets": [dataset]})
    with (
        _sync_lock(tmp_path, source.cognee_pipeline_scope),
        pytest.raises(Exception, match="already running"),
    ):
        await sync_mixpanel(source)


@pytest.mark.parametrize(
    "kwargs",
    [
        {"report_ids": [101, "bad"]},
        {"resources": "report"},
        {"schema_names": "Signup"},
        {"cohort_ids": [True]},
        {"resources": ["report", None]},
        {"report_types": {"type": "funnels"}},
        {"project_id": "456"},
    ],
)
def test_invalid_configuration_has_actionable_error(tmp_path, report, kwargs):
    with pytest.raises(ValueError):
        mixpanel_source(client=FakeClient(tmp_path, report), **kwargs)


def test_reused_pipeline_scope_cannot_delete_other_project(tmp_path, report):
    pipeline = pipeline_at(tmp_path)
    first = FakeClient(tmp_path, report, scope="project-one")
    baseline, _ = run(pipeline, first)
    second = FakeClient(tmp_path, scope="project-two")
    with pytest.raises(Exception, match="different access scope"):
        run(pipeline, second)
    with pipeline.sql_client() as sql:
        assert (
            sql.execute_sql(f"SELECT id, title, content FROM {MIXPANEL_TABLE_NAME} ORDER BY id")
            == baseline
        )
    assert second.reads == 0


def test_missing_new_selection_aborts_but_known_deleted_selection_can_reconcile(tmp_path, report):
    client = FakeClient(tmp_path, report)
    pipeline = pipeline_at(tmp_path)
    baseline, _ = run(pipeline, client, report_ids=[101])
    with pytest.raises(Exception, match="never been visible"):
        run(pipeline, client, report_ids=[999])
    assert run(pipeline, client, report_ids=[101])[0] == baseline
    client.data["report"] = []
    assert run(pipeline, client, report_ids=[101])[0] == []
    assert run(pipeline, client, report_ids=[101])[0] == []


def test_unknown_schema_type_aborts_instead_of_confirming_deletion(tmp_path):
    client = FakeClient(tmp_path)
    client.data["schema"] = [{"name": "Signup", "entityType": "unexpected"}]
    with pytest.raises(Exception, match="unknown entity type"):
        list(mixpanel_source(client=client))


@pytest.mark.asyncio
async def test_cancelled_sync_retains_lock_until_worker_finishes(tmp_path, report, monkeypatch):
    import cognee

    started, finish = asyncio.Event(), asyncio.Event()
    calls = []

    async def add(*args, **kwargs):
        started.set()
        await finish.wait()
        calls.append("add-finished")

    async def cognify(**kwargs):
        calls.append("cognify-finished")

    monkeypatch.setattr(cognee, "add", add)
    monkeypatch.setattr(cognee, "cognify", cognify)
    client = FakeClient(tmp_path, report)
    task = asyncio.create_task(sync_mixpanel(mixpanel_source(client=client)))
    await started.wait()
    task.cancel()
    await asyncio.sleep(0)
    task.cancel()  # Repeated cancellation must not release the lock either.
    await asyncio.sleep(0)
    with pytest.raises(Exception, match="already running"):
        await sync_mixpanel(mixpanel_source(client=client))
    assert not task.done()
    finish.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert calls == ["add-finished", "cognify-finished"]
    await sync_mixpanel(mixpanel_source(client=client))


def test_real_http_sdk_to_dlt_staging_path(tmp_path, report, cohort, schema):
    import httpx

    from cognee_community_connector_mixpanel import MixpanelClient

    requested = []

    def handler(request):
        requested.append(request.url.path)
        route = request.url.path
        body = {
            "/api/app/projects/metadata/index": {"123": {"role": "analyst"}},
            "/api/app/projects/123/schemas": [schema],
            "/api/app/projects/123/bookmarks": [report],
            "/api/app/projects/123/bookmarks/101": report,
            "/api/app/projects/123/cohorts": [cohort],
            "/api/app/projects/123/cohorts/202": cohort,
        }[route]
        return httpx.Response(200, json={"status": "ok", "results": body})

    client = MixpanelClient(
        project_id="123",
        username="test",
        secret="synthetic",
        state_directory=tmp_path,
        transport=httpx.MockTransport(handler),
    )
    pipeline = pipeline_at(tmp_path)
    assert len(run(pipeline, client)[0]) == 3
    assert len(run(pipeline, client)[0]) == 3
    assert len(requested) == 20  # Two complete, uncached captures through the pinned SDK.
