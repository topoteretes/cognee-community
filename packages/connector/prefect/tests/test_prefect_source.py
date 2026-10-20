"""Tests for the Prefect sync walker and row emission contract."""

from fake_prefect import FakePrefect

from cognee_community_connector_prefect.prefect import (
    PrefectRateLimitedError,
    _infer_ui_url,
    _iter_rows,
)

ROW_COLUMNS = {"id", "title", "content", "url", "_deleted"}


def _sync(fake: FakePrefect, state: dict, **kwargs) -> tuple[list[dict], dict]:
    stats: dict[str, int] = {}
    ui_url = _infer_ui_url(fake.base_url)
    rows = list(_iter_rows(fake, ui_url, state, stats, **kwargs))
    return rows, stats


def _workspace() -> FakePrefect:
    fake = FakePrefect()
    fake.add_flow("f1", "Data Ingestion Pipeline", tags=["etl", "daily"])
    fake.add_deployment(
        "d1",
        "Daily S3 Loader",
        "f1",
        tags=["prod"],
        parameters={"s3_bucket": "my-bucket", "aws_secret_key": "SUPER_SECRET_VALUE"},
    )
    fake.add_flow_run(
        "r1",
        "daily-s3-loader-run-1",
        "f1",
        deployment_id="d1",
        state_type="COMPLETED",
        state_name="Completed",
        parameters={"aws_secret_key": "SUPER_SECRET_VALUE"},
    )
    return fake


def test_first_sync_emits_every_record_with_frozen_row_shape():
    fake = _workspace()
    rows, stats = _sync(fake, {})

    ids = {r["id"] for r in rows}
    assert ids == {"flow:f1", "deployment:d1", "flow_run:r1"}
    assert all(set(r) == ROW_COLUMNS for r in rows)
    assert all(r["_deleted"] is False and r["title"] and r["content"] for r in rows)

    urls = {r["id"]: r["url"] for r in rows}
    assert urls["flow:f1"] == "http://localhost:4200/flows/flow/f1"
    assert urls["deployment:d1"] == "http://localhost:4200/deployments/deployment/d1"
    assert urls["flow_run:r1"] == "http://localhost:4200/runs/flow-run/r1"
    assert stats["failed"] == 0


def test_parameters_are_strictly_redacted_for_secrets():
    fake = _workspace()
    rows, _ = _sync(fake, {})

    full_text = " ".join(f"{r['title']} {r['content']}" for r in rows)
    assert "SUPER_SECRET_VALUE" not in full_text
    # Parameter keys should be mentioned, but not sensitive values
    assert "aws_secret_key" in full_text
    assert "s3_bucket" in full_text


def test_cloud_base_url_infers_cloud_ui_url():
    cloud_url = "https://api.prefect.cloud/api/accounts/acc123/workspaces/ws456"
    inferred = _infer_ui_url(cloud_url)
    assert inferred == "https://app.prefect.cloud/account/acc123/workspace/ws456"

    fake = FakePrefect(base_url=cloud_url)
    fake.add_flow("f99", "Cloud Flow")
    rows, _ = _sync(fake, {})
    assert (
        rows[0]["url"] == "https://app.prefect.cloud/account/acc123/workspace/ws456/flows/flow/f99"
    )


def test_second_sync_skips_unchanged_records():
    fake = _workspace()
    state = {}
    rows1, stats1 = _sync(fake, state)
    assert len(rows1) == 3
    assert stats1["scanned"] == 3
    assert stats1["skipped"] == 0

    rows2, stats2 = _sync(fake, state)
    assert len(rows2) == 0
    assert stats2["scanned"] == 3
    assert stats2["skipped"] == 3


def test_flow_run_state_transition_triggers_reemission():
    fake = _workspace()
    state = {}
    _sync(fake, state)

    # Change flow run state from COMPLETED to FAILED with new timestamp
    fake.flow_runs["r1"]["state_name"] = "Failed"
    fake.flow_runs["r1"]["state_type"] = "FAILED"
    fake.flow_runs["r1"]["total_run_time"] = 48.2

    rows2, stats2 = _sync(fake, state)
    assert len(rows2) == 1
    assert rows2[0]["id"] == "flow_run:r1"
    assert "Failed" in rows2[0]["content"]
    assert stats2["skipped"] == 2
    assert stats2["scanned"] == 3


def test_filtering_by_flow_tags():
    fake = FakePrefect()
    fake.add_flow("f1", "ETL Flow", tags=["etl", "daily"])
    fake.add_flow("f2", "ML Flow", tags=["ml", "training"])

    rows, _ = _sync(fake, {}, flow_tags=["etl"])
    flow_ids = {r["id"] for r in rows if r["id"].startswith("flow:")}
    assert flow_ids == {"flow:f1"}


def test_filtering_by_deployment_tags():
    fake = FakePrefect()
    fake.add_flow("f1", "Flow 1")
    fake.add_deployment("d1", "Prod Deployment", "f1", tags=["prod"])
    fake.add_deployment("d2", "Dev Deployment", "f1", tags=["dev"])

    rows, _ = _sync(fake, {}, deployment_tags=["prod"])
    dep_ids = {r["id"] for r in rows if r["id"].startswith("deployment:")}
    assert dep_ids == {"deployment:d1"}


def test_filtering_by_flow_run_states():
    fake = FakePrefect()
    fake.add_flow("f1", "Flow 1")
    fake.add_flow_run("r1", "Run 1", "f1", state_type="COMPLETED")
    fake.add_flow_run("r2", "Run 2", "f1", state_type="FAILED")

    rows, _ = _sync(fake, {}, flow_run_states=["FAILED"])
    run_ids = {r["id"] for r in rows if r["id"].startswith("flow_run:")}
    assert run_ids == {"flow_run:r2"}


def test_pagination_fetches_all_pages():
    fake = FakePrefect()
    for i in range(125):
        fake.add_flow(f"flow_{i}", f"Flow {i}")

    rows, stats = _sync(fake, {})
    flow_ids = {r["id"] for r in rows}
    assert len(flow_ids) == 125
    assert stats["scanned"] == 125


def test_request_budget_cutoff():
    fake = FakePrefect()
    for i in range(10):
        fake.add_flow(f"flow_{i}", f"Flow {i}")

    state = {}
    _, stats = _sync(fake, state, max_requests=1)
    # 1 request made, walked some flows
    assert stats["failed"] == 1
    assert stats["failed_budget"] == 1


def test_rate_limited_cutoff():
    fake = FakePrefect()
    fake.add_flow("f1", "Flow 1")
    fake.fail_paths["/flows/filter"] = PrefectRateLimitedError("HTTP 429")

    state = {}
    rows, stats = _sync(fake, state)
    assert len(rows) == 0
    assert stats["failed"] == 1
    assert stats["failed_rate_limit"] == 1
