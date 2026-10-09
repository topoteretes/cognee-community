"""Unit tests for the Terraform Cloud connector.

The HCP Terraform / Terraform Cloud REST API is fully mocked via
``FakeTFCSession`` — no ``requests`` traffic and no live token are required, so
these run in CI. Coverage:

  - secret redaction scrubs private keys, AWS keys, URL creds, and assignments
  - JSON:API pagination follows ``links.next``
  - the incremental cursor reads run ``created-at``
  - full backfill yields every run and records the cursor + run→workspace map
  - incremental re-sync yields ONLY runs created since the cursor
  - a run new to the corpus but older than the cursor is still ingested
  - runs whose workspace is deleted become hard-delete markers (forget-on-delete)
  - a run merely ageing out of the recent-runs window is NOT deleted
  - an empty workspace sweep does not mass-delete and preserves state
  - plan logs are fetched, capped, and redacted (or skipped when disabled)
  - the dlt resource is wired with merge + id PK + the hard_delete column
  - a real dlt merge removes the marked row (end-to-end forget-on-delete)
"""

import re

import pytest

from cognee_community_connector_terraform_cloud.terraform_cloud import (
    _paginate,
    _run_created_at,
    redact_secrets,
    sync_runs,
    terraform_cloud_source,
)

BASE_URL = "https://app.terraform.io/api/v2"
ORG = "acme"


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------
def _workspace(ws_id, name, *, tf_version="1.7.0"):
    return {
        "id": ws_id,
        "type": "workspaces",
        "attributes": {"name": name, "terraform-version": tf_version},
    }


def _run(run_id, *, created_at, status="applied", message="", plan_id=None):
    run = {
        "id": run_id,
        "type": "runs",
        "attributes": {"status": status, "created-at": created_at, "message": message},
        "relationships": {},
    }
    if plan_id:
        run["relationships"]["plan"] = {"data": {"id": plan_id, "type": "plans"}}
    return run


class _Resp:
    def __init__(self, *, payload=None, text=""):
        self._payload = payload
        self.text = text
        self.headers = {}

    def raise_for_status(self):
        pass

    def json(self):
        return self._payload


class FakeTFCSession:
    """Minimal stand-in for a ``requests`` session hitting TFC v2 (JSON:API)."""

    def __init__(self, workspaces, runs_by_ws, plan_logs=None):
        # workspaces: [ws, ...]; runs_by_ws: {ws_id: [run, ...]};
        # plan_logs: {plan_id: raw_log_text}
        self.workspaces = workspaces
        self.runs_by_ws = runs_by_ws
        self.plan_logs = plan_logs or {}
        self.calls = []

    def get(self, url, params=None):
        self.calls.append((url, params or {}))

        if url.endswith(f"/organizations/{ORG}/workspaces"):
            return _Resp(payload={"data": self.workspaces, "links": {}})

        m = re.search(r"/workspaces/([^/]+)/runs$", url)
        if m:
            return _Resp(payload={"data": self.runs_by_ws.get(m.group(1), []), "links": {}})

        m = re.search(r"/plans/([^/]+)$", url)
        if m:
            plan_id = m.group(1)
            log_url = f"https://archivist.example/{plan_id}"
            return _Resp(payload={"data": {"id": plan_id, "attributes": {"log-read-url": log_url}}})

        m = re.search(r"archivist\.example/([^/]+)$", url)
        if m:
            return _Resp(text=self.plan_logs.get(m.group(1), ""))

        raise AssertionError(f"unexpected URL: {url}")


def _single_ws_session(runs, *, plan_logs=None):
    return FakeTFCSession(
        workspaces=[_workspace("ws-1", "prod")],
        runs_by_ws={"ws-1": runs},
        plan_logs=plan_logs,
    )


# ---------------------------------------------------------------------------
# Secret redaction
# ---------------------------------------------------------------------------
def test_redact_handles_empty():
    assert redact_secrets("") == ""
    assert redact_secrets(None) == ""


def test_redact_private_key_block():
    log = (
        "-----BEGIN RSA PRIVATE KEY-----\n"
        "MIIEpAIBAAKCAQEA...\nabc123\n"
        "-----END RSA PRIVATE KEY-----"
    )
    assert redact_secrets(log) == "[REDACTED PRIVATE KEY]"


def test_redact_aws_access_key():
    assert "[REDACTED]" in redact_secrets("key = AKIAIOSFODNN7EXAMPLE")
    assert "AKIA" not in redact_secrets("prefix AKIAIOSFODNN7EXAMPLE suffix")


def test_redact_url_credentials_keeps_host():
    out = redact_secrets("postgres://admin:s3cr3tP@db.internal:5432/app")
    assert "s3cr3tP" not in out
    assert "admin:[REDACTED]@db.internal" in out


def test_redact_sensitive_assignments_various_shapes():
    assert redact_secrets('api_key = "abc123"') == "api_key = [REDACTED]"
    assert redact_secrets("db_password: hunter2") == "db_password: [REDACTED]"
    assert redact_secrets('"client_secret" = "xyz"') == '"client_secret" = [REDACTED]'
    # The sensitive key name is preserved so the log still reads naturally.
    assert "access_key" in redact_secrets("access_key=AKIAIOSFODNN7EXAMPLE")


def test_redact_leaves_ordinary_plan_text_intact():
    plan = (
        'aws_instance.web: Creating...\n+ resource "aws_s3_bucket" "logs" {\n  bucket = "my-logs"'
    )
    assert redact_secrets(plan) == plan


# ---------------------------------------------------------------------------
# Pagination / cursor helpers
# ---------------------------------------------------------------------------
def test_paginate_follows_links_next():
    pages = {
        f"{BASE_URL}/x": {"data": [{"id": "a"}], "links": {"next": f"{BASE_URL}/x?page=2"}},
        f"{BASE_URL}/x?page=2": {"data": [{"id": "b"}], "links": {}},
    }

    class S:
        def get(self, url, params=None):
            return _Resp(payload=pages[url])

    items = list(_paginate(S(), BASE_URL, "/x", {}))
    assert [i["id"] for i in items] == ["a", "b"]


def test_run_created_at_reads_attribute():
    assert (
        _run_created_at(_run("run-1", created_at="2024-05-01T00:00:00Z")) == "2024-05-01T00:00:00Z"
    )
    assert _run_created_at({}) == ""


# ---------------------------------------------------------------------------
# sync_runs — backfill / incremental / deletion
# ---------------------------------------------------------------------------
def test_backfill_yields_all_runs_and_records_cursor_and_map():
    session = _single_ws_session(
        [
            _run("run-2", created_at="2024-01-02T10:00:00Z", status="applied"),
            _run("run-1", created_at="2024-01-01T10:00:00Z", status="errored"),
        ]
    )
    state = {}
    rows = list(sync_runs(session, BASE_URL, ORG, state, include_plan_logs=False))

    assert {r["id"] for r in rows} == {"run-1", "run-2"}
    assert all(r["_deleted"] is False for r in rows)
    assert {r["workspace"] for r in rows} == {"prod"}
    # Cursor + run→workspace map captured for the next incremental run.
    assert state["last_created_at"] == "2024-01-02T10:00:00Z"
    assert state["known_runs"] == {"run-1": "ws-1", "run-2": "ws-1"}
    # Human-facing run URL is reconstructed for citations.
    assert rows[0]["url"].startswith(f"https://app.terraform.io/app/{ORG}/workspaces/prod/runs/")


def test_incremental_yields_only_runs_since_cursor():
    session = _single_ws_session(
        [
            _run("run-2", created_at="2024-02-01T10:00:00Z"),
            _run("run-1", created_at="2024-01-01T10:00:00Z"),
        ]
    )
    state = {"known_runs": {"run-1": "ws-1"}, "last_created_at": "2024-01-01T10:00:00Z"}
    rows = list(sync_runs(session, BASE_URL, ORG, state, include_plan_logs=False))

    assert [r["id"] for r in rows] == ["run-2"]  # only the run newer than the cursor
    assert state["last_created_at"] == "2024-02-01T10:00:00Z"


def test_incremental_no_changes_is_a_noop():
    session = _single_ws_session([_run("run-1", created_at="2024-01-01T10:00:00Z")])
    state = {"known_runs": {"run-1": "ws-1"}, "last_created_at": "2024-01-01T10:00:00Z"}
    rows = list(sync_runs(session, BASE_URL, ORG, state, include_plan_logs=False))
    assert rows == []


def test_new_run_below_cursor_is_still_ingested():
    # A workspace just added to the selection brings runs older than the cursor;
    # a run new to the corpus is ingested regardless of timestamp, while an
    # already-known run is skipped.
    session = FakeTFCSession(
        workspaces=[_workspace("ws-1", "prod"), _workspace("ws-2", "staging")],
        runs_by_ws={
            "ws-1": [_run("run-1", created_at="2024-05-01T00:00:00Z")],  # known
            "ws-2": [_run("run-9", created_at="2024-01-01T00:00:00Z")],  # new, but old
        },
    )
    state = {"known_runs": {"run-1": "ws-1"}, "last_created_at": "2024-05-01T00:00:00Z"}
    rows = list(sync_runs(session, BASE_URL, ORG, state, include_plan_logs=False))

    assert [r["id"] for r in rows] == ["run-9"]  # run-1 skipped, run-9 backfilled


def test_deleted_workspace_emits_hard_delete_markers():
    # run-2 belonged to ws-2, which is gone from the org now.
    session = FakeTFCSession(
        workspaces=[_workspace("ws-1", "prod")],
        runs_by_ws={"ws-1": [_run("run-1", created_at="2024-01-01T10:00:00Z")]},
    )
    state = {
        "known_runs": {"run-1": "ws-1", "run-2": "ws-2"},
        "last_created_at": "2024-01-01T10:00:00Z",
    }
    rows = list(sync_runs(session, BASE_URL, ORG, state, include_plan_logs=False))

    assert rows == [{"id": "run-2", "_deleted": True}]
    assert state["known_runs"] == {"run-1": "ws-1"}  # map now reflects reality


def test_run_ageing_out_of_window_is_not_deleted():
    # The workspace still exists but its only recent run (window=1) is newer than
    # the previously-known run-1. run-1 drops out of the window but MUST NOT be
    # treated as deleted, because its workspace is still present.
    session = _single_ws_session(
        [
            _run("run-2", created_at="2024-02-01T10:00:00Z"),
            _run("run-1", created_at="2024-01-01T10:00:00Z"),
        ]
    )
    state = {"known_runs": {"run-1": "ws-1"}, "last_created_at": "2024-01-01T10:00:00Z"}
    rows = list(
        sync_runs(session, BASE_URL, ORG, state, include_plan_logs=False, max_runs_per_workspace=1)
    )

    # Only the new run is emitted; no hard-delete marker for run-1.
    assert [r.get("id") for r in rows] == ["run-2"]
    assert all(not r.get("_deleted") for r in rows)
    assert "run-1" in state["known_runs"]


def test_empty_sweep_does_not_mass_delete_and_preserves_state():
    # A sweep that returns zero workspaces while runs were known is treated as a
    # transient failure, NOT "everything deleted".
    session = FakeTFCSession(workspaces=[], runs_by_ws={})
    state = {
        "known_runs": {"run-1": "ws-1", "run-2": "ws-1"},
        "last_created_at": "2024-01-01T10:00:00Z",
    }
    rows = list(sync_runs(session, BASE_URL, ORG, state, include_plan_logs=False))

    assert rows == []  # no hard-delete markers emitted
    assert state["known_runs"] == {"run-1": "ws-1", "run-2": "ws-1"}  # map preserved


def test_workspace_names_filter_is_applied():
    session = FakeTFCSession(
        workspaces=[_workspace("ws-1", "prod"), _workspace("ws-2", "staging")],
        runs_by_ws={
            "ws-1": [_run("run-1", created_at="2024-01-01T10:00:00Z")],
            "ws-2": [_run("run-2", created_at="2024-01-02T10:00:00Z")],
        },
    )
    rows = list(
        sync_runs(session, BASE_URL, ORG, {}, workspace_names=["prod"], include_plan_logs=False)
    )
    assert [r["id"] for r in rows] == ["run-1"]  # staging excluded


# ---------------------------------------------------------------------------
# Plan logs
# ---------------------------------------------------------------------------
def test_plan_log_is_fetched_capped_and_redacted():
    secret_log = "Plan: 1 to add\naws_key = AKIAIOSFODNN7EXAMPLE\n" + ("x" * 50_000)
    session = _single_ws_session(
        [_run("run-1", created_at="2024-01-01T10:00:00Z", plan_id="plan-1")],
        plan_logs={"plan-1": secret_log},
    )
    rows = list(sync_runs(session, BASE_URL, ORG, {}, max_plan_log_chars=100))

    body = rows[0]["body"]
    assert "AKIAIOSFODNN7EXAMPLE" not in body  # redacted
    assert "[REDACTED]" in body
    assert "plan log truncated" in body  # capped
    assert "## Plan output" in body


def test_plan_log_skipped_when_disabled():
    session = _single_ws_session(
        [_run("run-1", created_at="2024-01-01T10:00:00Z", plan_id="plan-1")],
        plan_logs={"plan-1": "some log"},
    )
    rows = list(sync_runs(session, BASE_URL, ORG, {}, include_plan_logs=False))
    assert "## Plan output" not in rows[0]["body"]
    # No plan endpoints were hit when logs are disabled.
    assert not any("/plans/" in url for url, _ in session.calls)


# ---------------------------------------------------------------------------
# terraform_cloud_source — dlt wiring — requires dlt
# ---------------------------------------------------------------------------
def test_source_resource_is_configured_for_merge_and_hard_delete():
    pytest.importorskip("dlt")

    resource = terraform_cloud_source(organization=ORG, session=_single_ws_session([]))
    assert resource.name == "terraform_cloud_runs"

    schema = resource.compute_table_schema()
    write_disposition = schema.get("write_disposition")
    if isinstance(write_disposition, dict):
        write_disposition = write_disposition.get("disposition")
    assert write_disposition == "merge"

    columns = schema["columns"]
    assert columns["id"].get("primary_key") is True
    assert columns["_deleted"].get("hard_delete") is True


def test_source_requires_token_or_session(monkeypatch):
    pytest.importorskip("dlt")
    monkeypatch.delenv("TFC_TOKEN", raising=False)
    monkeypatch.delenv("TERRAFORM_CLOUD_TOKEN", raising=False)
    with pytest.raises(ValueError, match="token"):
        terraform_cloud_source(organization=ORG)


def test_source_reads_token_from_env(monkeypatch):
    pytest.importorskip("dlt")
    monkeypatch.setenv("TFC_TOKEN", "secret-token")
    # Should not raise even though no session is injected.
    resource = terraform_cloud_source(organization=ORG)
    assert resource.name == "terraform_cloud_runs"


# ---------------------------------------------------------------------------
# End-to-end: a real dlt merge acts on the hard-delete marker
# ---------------------------------------------------------------------------
def test_forget_on_delete_end_to_end_through_a_real_dlt_merge(tmp_path):
    dlt = pytest.importorskip("dlt")
    pytest.importorskip("duckdb")

    pipeline = dlt.pipeline(
        pipeline_name="test_tfc_e2e",
        destination=dlt.destinations.duckdb(str(tmp_path / "tfc.duckdb")),
        dataset_name="terraform",
    )

    # Sync #1: two workspaces, one run each, both land in the destination.
    session1 = FakeTFCSession(
        workspaces=[_workspace("ws-1", "prod"), _workspace("ws-2", "staging")],
        runs_by_ws={
            "ws-1": [_run("run-1", created_at="2024-01-01T10:00:00Z")],
            "ws-2": [_run("run-2", created_at="2024-01-02T10:00:00Z")],
        },
    )
    pipeline.run(
        terraform_cloud_source(organization=ORG, session=session1, include_plan_logs=False)
    )
    with pipeline.sql_client() as client:
        assert client.execute_sql("SELECT count(*) FROM terraform_cloud_runs")[0][0] == 2

    # Sync #2: the staging workspace is deleted upstream. The connector emits a
    # hard-delete marker for run-2; dlt's merge removes it from the destination.
    session2 = FakeTFCSession(
        workspaces=[_workspace("ws-1", "prod")],
        runs_by_ws={"ws-1": [_run("run-1", created_at="2024-01-01T10:00:00Z")]},
    )
    pipeline.run(
        terraform_cloud_source(organization=ORG, session=session2, include_plan_logs=False)
    )
    with pipeline.sql_client() as client:
        rows = client.execute_sql("SELECT id FROM terraform_cloud_runs")
    assert [r[0] for r in rows] == ["run-1"]  # run-2 forgotten, run-1 retained
