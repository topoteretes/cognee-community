"""Unit and dlt-pipeline tests for the Terraform Cloud connector. No live account needed.

* Client tests run ``TerraformCloudClient`` against ``httpx.MockTransport``.
* Log tests cover plain text and structured run output.
* Sync tests drive ``_iter_rows`` with a fake API and a plain dict as state, and
  run ``terraform_cloud_source`` through a dlt pipeline into a temp sqlite
  destination to prove the cursor and forget-on-delete.
"""

import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import NAMESPACE_OID, uuid5

import httpx
import pytest
from cognee.tasks.ingestion import dlt_utils
from cognee.tasks.ingestion.resolve_dlt_sources import _build_document_data_item
from conftest import ORG

from cognee_community_connector_terraform_cloud.terraform_cloud import (
    TerraformCloudAPIError,
    TerraformCloudAuthError,
    TerraformCloudClient,
    _iter_rows,
    _TerraformCloudConfig,
    render_workspace,
    summarize_log,
    terraform_cloud_source,
)

NOW = datetime(2026, 10, 16, 12, 0, tzinfo=UTC)
AWS_KEY = "AKIA" + "IOSFODNN7EXAMPLE"

TEXT_LOG = (
    "Terraform v1.9.4\n"
    "\x1b[1m  # aws_s3_bucket.logs\x1b[0m will be created\n"
    '  + resource "aws_s3_bucket" "logs" {\n'
    f'      + secret_key = "{AWS_KEY}"\n'
    "    }\n"
    "  # aws_db_instance.main must be replaced\n"
    "\x1b[1mPlan:\x1b[0m 1 to add, 0 to change, 1 to destroy.\n"
)
ERROR_LOG = "╷\n│ Error: Invalid value for variable\n│ \n│ The given value is not valid.\n╵\n"


def _hours_ago(hours):
    return (NOW - timedelta(hours=hours)).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def _json_log(*events):
    return "\n".join(json.dumps(event) for event in events)


def _config(**overrides):
    values = {
        "organization": ORG,
        "hostname": "app.terraform.io",
        "workspace_names": (),
        "workspace_wildcard": None,
        "workspace_tags": (),
        "project_id": None,
        "include_speculative": False,
        "include_plan_output": True,
        "since_days": 90,
    }
    values.update(overrides)
    return _TerraformCloudConfig(**values)


def _stats():
    return {"emitted": 0, "deleted": 0, "pending": 0, "redacted": 0}


def _run(tfc, state, stats=None, **overrides):
    stats = stats if stats is not None else _stats()
    return list(_iter_rows(tfc, _config(**overrides), state, stats, now=lambda: NOW))


def _ids(rows):
    return sorted(row["id"] for row in rows)


@pytest.fixture
def org(tfc):
    tfc.add_workspace("ws-1", "networking-prod", tags=["prod"])
    tfc.add_workspace("ws-2", "app-staging", tags=["staging"])
    return tfc


# ---------------------------------------------------------------------------
# TerraformCloudClient over httpx.MockTransport
# ---------------------------------------------------------------------------
def _client(responses):
    queue = list(responses)
    requests, sleeps = [], []

    def handler(request):
        requests.append(request)
        return queue.pop(0) if len(queue) > 1 else queue[0]

    client = TerraformCloudClient(
        "tok-1",
        http=httpx.Client(transport=httpx.MockTransport(handler)),
        sleep=sleeps.append,
    )
    return client, requests, sleeps


def test_client_sends_bearer_token_to_the_api_only():
    client, requests, _ = _client([httpx.Response(200, json={"data": {}})])
    client.get(f"/organizations/{ORG}")
    client.download("https://archivist.test/object/secret")
    api, blob = requests
    assert str(api.url) == f"https://app.terraform.io/api/v2/organizations/{ORG}"
    assert api.headers["Authorization"] == "Bearer tok-1"
    assert api.headers["Content-Type"] == "application/vnd.api+json"
    assert "Authorization" not in blob.headers


def test_client_retries_rate_limits_and_server_errors():
    client, _, sleeps = _client(
        [
            httpx.Response(429, headers={"Retry-After": "7"}),
            httpx.Response(503),
            httpx.Response(200, json={"data": []}),
        ]
    )
    assert client.get("/organizations/acme/runs") == {"data": []}
    assert sleeps == [7.0, 2.0]


def test_client_errors_carry_status_only():
    client, _, _ = _client([httpx.Response(401, json={"errors": [{"detail": "bad"}]})])
    with pytest.raises(TerraformCloudAuthError):
        client.get("/organizations/acme")
    client, _, _ = _client([httpx.Response(404, json={"errors": [{"detail": "acme"}]})])
    with pytest.raises(TerraformCloudAPIError) as info:
        client.get("/organizations/acme")
    assert info.value.status == 404 and "acme" not in str(info.value)


def test_client_rejects_bad_tokens_without_echoing_them():
    with pytest.raises(ValueError) as info:
        TerraformCloudClient("s3cr3t token")
    assert "s3cr3t" not in str(info.value)
    assert "s3cr3t" not in repr(TerraformCloudClient("s3cr3t"))


# ---------------------------------------------------------------------------
# Log summaries and rendering
# ---------------------------------------------------------------------------
def test_text_log_summary_keeps_actions_not_values():
    lines = summarize_log(TEXT_LOG + ERROR_LOG)
    assert lines == [
        "Planned changes:",
        "- aws_s3_bucket.logs will be created",
        "- aws_db_instance.main must be replaced",
        "Plan: 1 to add, 0 to change, 1 to destroy.",
        "Diagnostics:",
        "Error: Invalid value for variable",
    ]
    assert AWS_KEY not in "\n".join(lines)


def test_structured_log_summary():
    log = _json_log(
        {"type": "version", "@message": "Terraform 1.9.4"},
        {
            "type": "planned_change",
            "change": {"resource": {"addr": "aws_vpc.main"}, "action": "update"},
        },
        {
            "type": "planned_change",
            "change": {"resource": {"addr": "null_resource.x"}, "action": "noop"},
        },
        {"type": "change_summary", "@message": "Plan: 0 to add, 1 to change, 0 to destroy."},
        {"type": "diagnostic", "diagnostic": {"severity": "warning", "summary": "Deprecated"}},
    )
    assert summarize_log(log) == [
        "Planned changes:",
        "- aws_vpc.main will be updated in-place",
        "Plan: 0 to add, 1 to change, 0 to destroy.",
        "Diagnostics:",
        "Warning: Deprecated",
    ]


def test_long_plans_are_capped():
    log = "\n".join(f"  # null_resource.n[{i}] will be created" for i in range(205))
    lines = summarize_log(log)
    assert lines[-1] == "- ... and 5 more" and len(lines) == 202


def test_workspace_document_skips_fields_that_change_on_every_run(org):
    workspace = org.workspaces["ws-1"]
    before = render_workspace(workspace, ORG, "app.terraform.io")
    workspace["attributes"].update({"resource-count": 99, "locked": True})
    assert render_workspace(workspace, ORG, "app.terraform.io") == before
    assert before["content"] == (
        "Workspace: networking-prod\nOrganization: acme\nTerraform version: 1.9.4\n"
        "Execution mode: remote\nTags: prod"
    )
    assert before["url"] == "https://app.terraform.io/app/acme/workspaces/networking-prod"


def test_row_becomes_a_terraform_cloud_document(org):
    row = render_workspace(org.workspaces["ws-1"], ORG, "app.terraform.io")
    dlt_row = SimpleNamespace(
        table_name="terraform_cloud", primary_key_value=row["id"], row_data=row, content_hash="h"
    )
    item = _build_document_data_item(dlt_row, uuid5(NAMESPACE_OID, row["id"]), "terraform_cloud")
    assert item.data.startswith("# Workspace networking-prod")
    assert item.system_metadata["source"] == "terraform_cloud"


# ---------------------------------------------------------------------------
# Sync state machine (_iter_rows with a dict as state)
# ---------------------------------------------------------------------------
def test_first_sync_emits_workspaces_and_finished_runs(org):
    org.add_run(
        "ws-1",
        "run-1",
        "2026-10-15T10:00:00.000Z",
        plan_log=TEXT_LOG,
        commit={
            "commit-sha": "1e1c1018d1bbc0b8",
            "branch": "main",
            "commit-message": "Add logs bucket",
        },
    )
    org.add_run("ws-2", "run-2", "2026-10-15T11:00:00.000Z", status="planning")
    state = {}
    rows = {row["id"]: row for row in _run(org, state)}
    assert set(rows) == {"workspace:ws-1", "workspace:ws-2", "run:run-1"}
    assert rows["run:run-1"]["content"] == (
        "Workspace: networking-prod\nStatus: applied\nOperation: plan and apply\n"
        "Trigger: manual\nSource: tfe-ui\nCreated: 2026-10-15 10:00 UTC\n"
        "Message: Triggered via UI\nCommit: 1e1c1018d1bb on main: Add logs bucket\n"
        "Plan: 2 to add, 1 to change, 0 to destroy\nApply: 2 added, 1 changed, 0 destroyed\n"
        "\nPlanned changes:\n- aws_s3_bucket.logs will be created\n"
        "- aws_db_instance.main must be replaced\nPlan: 1 to add, 0 to change, 1 to destroy."
    )
    assert state["pending"] == {"run-2": "ws-2"}


def test_no_change_sync_emits_nothing_and_stops_at_the_cursor(org):
    for hour in range(250):
        org.add_run("ws-1", f"run-{hour:03d}", _hours_ago(250 - hour))
    state = {}
    _run(org, state)
    org.calls.clear()

    assert _run(org, state) == []
    assert [p["page[number]"] for p in org.run_list_calls()] == [1]


def test_unordered_answers_disable_the_early_stop(org):
    for hour in range(150):
        org.add_run("ws-1", f"run-{hour:03d}", _hours_ago(150 - hour))
    state = {}
    _run(org, state)
    org.unordered = True
    org.calls.clear()
    _run(org, state)
    assert [p["page[number]"] for p in org.run_list_calls()] == [1, 2]


def test_new_run_is_emitted_and_moves_the_cursor(org):
    org.add_run("ws-1", "run-1", "2026-10-15T10:00:00.000Z")
    state = {}
    _run(org, state)
    org.add_run("ws-1", "run-2", "2026-10-16T09:00:00.000Z")
    assert _ids(_run(org, state)) == ["run:run-2"]
    assert state["cursor"] == "2026-10-16T09:00:00.000Z"


def test_run_in_progress_is_emitted_once_it_finishes(org):
    org.add_run("ws-1", "run-1", "2026-10-01T10:00:00.000Z", status="planning")
    org.add_run("ws-1", "run-2", "2026-10-15T10:00:00.000Z")
    state = {}
    _run(org, state)

    # Now below the cursor, so it is found through the pending check, not the listing.
    org.add_run("ws-1", "run-1", "2026-10-01T10:00:00.000Z", status="errored", plan_log=ERROR_LOG)
    rows = _run(org, state)
    assert _ids(rows) == ["run:run-1"]
    assert "Error: Invalid value for variable" in rows[0]["content"]
    assert state["pending"] == {}


def test_pending_run_that_disappears_is_dropped(org):
    org.add_run("ws-1", "run-1", "2026-10-15T10:00:00.000Z", status="planning")
    state = {}
    _run(org, state)
    del org.runs["run-1"]
    assert _run(org, state) == []
    assert state["pending"] == {}


def test_apply_log_is_read_only_for_failed_applies(org):
    org.add_run("ws-1", "run-1", "2026-10-15T10:00:00.000Z", plan_log=TEXT_LOG)
    org.add_run(
        "ws-1",
        "run-2",
        "2026-10-15T11:00:00.000Z",
        status="errored",
        plan_log=TEXT_LOG,
        apply_log=ERROR_LOG,
    )
    org.add_run("ws-1", "run-3", "2026-10-15T12:00:00.000Z", status="errored", plan_log=ERROR_LOG)
    rows = {row["id"]: row for row in _run(org, {})}
    assert "https://archivist.test/apply-run-1" not in org.downloads
    assert "https://archivist.test/apply-run-3" not in org.downloads
    assert "Error: Invalid value for variable" in rows["run:run-2"]["content"]


def test_speculative_runs_are_opt_in(org):
    org.add_run(
        "ws-1",
        "run-1",
        "2026-10-15T10:00:00.000Z",
        operation="plan_only",
        status="planned_and_finished",
    )
    assert _ids(_run(org, {})) == ["workspace:ws-1", "workspace:ws-2"]
    rows = _run(org, {}, include_speculative=True)
    assert "Operation: plan only" in next(r for r in rows if r["id"] == "run:run-1")["content"]


def test_plan_output_can_be_turned_off(org):
    org.add_run("ws-1", "run-1", "2026-10-15T10:00:00.000Z", plan_log=TEXT_LOG)
    _run(org, {}, include_plan_output=False)
    assert org.downloads == []


def test_secrets_are_scrubbed_from_every_field(org):
    org.add_workspace("ws-3", "secrets", description=f"Uses {AWS_KEY}")
    org.add_run(
        "ws-3",
        "run-1",
        "2026-10-15T10:00:00.000Z",
        message="rotate password: hunter2hunter2",
        plan_log="│ Error: auth failed for postgres://admin:pa55word@db/app\n",
        commit={"commit-sha": "abc", "commit-message": f"use {AWS_KEY}"},
    )
    stats = _stats()
    text = json.dumps(_run(org, {}, stats=stats))
    for secret in (AWS_KEY, "hunter2hunter2", "pa55word"):
        assert secret not in text
    assert stats["redacted"] >= 4


def test_deleted_workspace_forgets_its_runs(org):
    org.add_run("ws-2", "run-1", "2026-10-15T10:00:00.000Z")
    state = {}
    _run(org, state)
    del org.workspaces["ws-2"]
    assert _run(org, state) == [
        {"id": "run:run-1", "_deleted": True},
        {"id": "workspace:ws-2", "_deleted": True},
    ]


def test_selection_by_tags_forgets_workspaces_left_out(org):
    org.add_run("ws-2", "run-1", "2026-10-15T10:00:00.000Z")
    state = {}
    _run(org, state)
    rows = _run(org, state, workspace_tags=("prod",))
    assert _ids(rows) == ["run:run-1", "workspace:ws-2"]
    assert all(row["_deleted"] for row in rows)


def test_many_selected_workspaces_are_listed_in_groups(tfc):
    for index in range(30):
        tfc.add_workspace(f"ws-{index}", f"svc-{index}")
    _run(tfc, {}, workspace_names=tuple(f"svc-{index}" for index in range(30)))
    groups = [p["filter[workspace_names]"].split(",") for p in tfc.run_list_calls()]
    assert [len(group) for group in groups] == [25, 5]


def test_unknown_organization_and_bad_token_raise(org):
    with pytest.raises(TerraformCloudAPIError, match="not found"):
        _run(org, {}, organization="nope")
    org.errors[f"/organizations/{ORG}"] = 500
    with pytest.raises(TerraformCloudAPIError):
        _run(org, {})


# ---------------------------------------------------------------------------
# terraform_cloud_source factory
# ---------------------------------------------------------------------------
def test_source_declares_document_path_and_scope(tfc):
    source = terraform_cloud_source(ORG, client=tfc, resource_name="tfc_acme")
    assert getattr(source, dlt_utils.DOCUMENT_SOURCE_ATTR) == "terraform_cloud"
    assert getattr(source, dlt_utils.PIPELINE_SCOPE_ATTR) == "tfc_acme"
    assert source.name == "tfc_acme"


def test_missing_settings_name_the_env_vars(monkeypatch):
    for name in ("TFE_ORGANIZATION", "TFE_TOKEN", "TFE_HOSTNAME"):
        monkeypatch.delenv(name, raising=False)
    with pytest.raises(ValueError, match="TFE_ORGANIZATION"):
        terraform_cloud_source()
    with pytest.raises(ValueError, match="TFE_TOKEN"):
        terraform_cloud_source(ORG)


# ---------------------------------------------------------------------------
# dlt pipeline: incremental cursor + forget-on-delete
# ---------------------------------------------------------------------------
@pytest.fixture
def dlt_mod():
    return pytest.importorskip("dlt")


def _pipeline(dlt, tmp_path):
    db_path = (tmp_path / "tfc.db").as_posix()
    return dlt.pipeline(
        pipeline_name="tfc_test",
        destination=dlt.destinations.sqlalchemy(f"sqlite:///{db_path}"),
        dataset_name="tfc_ds",
        pipelines_dir=str(tmp_path / "state"),
    )


def _staged(pipeline):
    with (
        pipeline.sql_client() as client,
        client.execute_query("SELECT id FROM terraform_cloud") as cursor,
    ):
        return {row[0] for row in cursor.fetchall()}


@pytest.fixture
def recent(monkeypatch):
    from cognee_community_connector_terraform_cloud import terraform_cloud as module

    monkeypatch.setattr(module, "_utcnow", lambda: NOW)


def test_pipeline_resyncs_only_changes_and_forgets_deletions(dlt_mod, tmp_path, org, recent):
    org.add_run("ws-1", "run-1", "2026-10-15T10:00:00.000Z")
    org.add_run("ws-2", "run-2", "2026-10-15T11:00:00.000Z")
    pipeline = _pipeline(dlt_mod, tmp_path)

    pipeline.run(terraform_cloud_source(ORG, client=org))
    assert _staged(pipeline) == {"workspace:ws-1", "workspace:ws-2", "run:run-1", "run:run-2"}

    # The cursor lives in dlt state, so a fresh source object resumes from it.
    source = terraform_cloud_source(ORG, client=org)
    pipeline.run(source)
    assert source.cognee_sync_stats["emitted"] == 0

    del org.workspaces["ws-2"]
    pipeline.run(terraform_cloud_source(ORG, client=org))
    assert _staged(pipeline) == {"workspace:ws-1", "run:run-1"}


def test_failed_run_keeps_staging_and_cursor(dlt_mod, tmp_path, org, recent):
    org.add_run("ws-2", "run-1", "2026-10-15T10:00:00.000Z")
    pipeline = _pipeline(dlt_mod, tmp_path)
    pipeline.run(terraform_cloud_source(ORG, client=org))

    del org.workspaces["ws-2"]
    org.errors["/runs"] = 500
    with pytest.raises(Exception, match="500"):
        pipeline.run(terraform_cloud_source(ORG, client=org))
    assert "run:run-1" in _staged(pipeline)

    org.errors.clear()
    pipeline.run(terraform_cloud_source(ORG, client=org))
    assert _staged(pipeline) == {"workspace:ws-1"}
