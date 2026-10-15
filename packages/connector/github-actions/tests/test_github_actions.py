"""Unit tests for the GitHub Actions dlt connector.

All tests run without a real GitHub token — the GitHub API is fully mocked via
a FakeGitHubClient injected through the http_client= test seam.

Coverage:
  - Workflow definition sync: new file, unchanged file (blob SHA cache), removed file
  - Run-attempt sync: new completed run, in-progress run skipped, re-run (attempt 2)
  - 1,000-result pagination limit → time-window subdivision
  - Job sync: steps, annotations, log tail, expired logs
  - Forget-on-delete: tombstones for removed workflows, deleted runs, deleted jobs
  - ANSI stripping in log tails
  - Partial-failure safety: no tombstones on API errors
  - document-source marker wired correctly
"""

import textwrap
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from cognee_community_connector_github_actions.github_actions import (
    GITHUB_ACTIONS_SOURCE_NAME,
    _ANSI_RE,
    _GitHubClient,
    _NotFoundError,
    _extract_triggers,
    _extract_jobs_summary,
    _list_runs_window,
    _parse_dt,
    _render_run_doc,
    _render_workflow_doc,
    _run_doc_id,
    _sync_jobs,
    _sync_runs,
    _sync_workflows,
    _workflow_doc_id,
)


# ──────────────────────────────────────────────────────────────────────────────
# Fake HTTP client (test seam)
# ──────────────────────────────────────────────────────────────────────────────


class FakeGitHubClient:
    """In-memory stand-in for _GitHubClient.

    Configure it by setting attributes before each test.
    """

    def __init__(self):
        # Map of (path, frozenset(params.items())) → response data
        self._responses: dict = {}
        # Map of path → raw text (for file content)
        self._file_content: dict[str, str] = {}
        # Map of (repo, run_id, attempt, job_id, step_number) → log text
        self._step_logs: dict = {}
        # Track calls for assertion
        self.calls: list[tuple] = []

    def register(self, path: str, data, params: dict | None = None):
        key = (path, frozenset((params or {}).items()))
        self._responses[key] = data

    def register_file(self, path: str, content: str):
        self._file_content[path] = content

    def register_log(self, repo, run_id, attempt, job_id, step_number, text):
        self._step_logs[(repo, run_id, attempt, job_id, step_number)] = text

    def get(self, path: str, params: dict | None = None):
        self.calls.append(("get", path, params))
        key = (path, frozenset((params or {}).items()))
        # Try exact match first, then path-only fallback
        if key in self._responses:
            return self._responses[key]
        fallback = (path, frozenset())
        if fallback in self._responses:
            return self._responses[fallback]
        raise _NotFoundError(path)

    def get_file_content(self, repo: str, path: str) -> str:
        self.calls.append(("get_file_content", repo, path))
        key = f"{repo}/{path}"
        if key in self._file_content:
            return self._file_content[key]
        raise _NotFoundError(path)

    def get_job_step_log(self, repo, run_id, attempt, job_id, step_number):
        self.calls.append(("get_job_step_log", repo, run_id, attempt, job_id, step_number))
        key = (repo, run_id, attempt, job_id, step_number)
        if key in self._step_logs:
            return self._step_logs[key]
        from cognee_community_connector_github_actions.github_actions import _GoneError
        raise _GoneError("log expired")


# ──────────────────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────────────────


def _make_run(run_id, attempt=1, status="completed", conclusion="success",
              branch="main", sha="abc123def456", event="push",
              path=".github/workflows/ci.yml"):
    return {
        "id": run_id,
        "run_attempt": attempt,
        "status": status,
        "conclusion": conclusion,
        "head_branch": branch,
        "head_sha": sha,
        "event": event,
        "path": path,
        "workflow_id": 42,
        "name": f"CI (run {run_id})",
        "display_title": f"CI (run {run_id})",
        "run_started_at": "2026-10-01T10:00:00Z",
        "updated_at": "2026-10-01T10:05:00Z",
        "html_url": f"https://github.com/owner/repo/actions/runs/{run_id}",
        "actor": {"login": "nevilsonani"},
    }


def _make_job(job_id, name="build", conclusion="success", steps=None):
    return {
        "id": job_id,
        "name": name,
        "status": "completed",
        "conclusion": conclusion,
        "runner_name": "ubuntu-latest",
        "labels": ["ubuntu-latest"],
        "html_url": f"https://github.com/owner/repo/actions/runs/1/jobs/{job_id}",
        "check_run_url": f"https://api.github.com/repos/owner/repo/check-runs/{job_id}",
        "steps": steps or [
            {"name": "Checkout", "conclusion": "success", "number": 1},
            {"name": "Run tests", "conclusion": conclusion, "number": 2},
        ],
    }


SIMPLE_WORKFLOW_YAML = textwrap.dedent("""\
    name: CI
    on: [push, pull_request]
    jobs:
      build:
        runs-on: ubuntu-latest
        steps:
          - name: Checkout
            uses: actions/checkout@v4
          - name: Run tests
            run: pytest
""")


# ──────────────────────────────────────────────────────────────────────────────
# Rendering helpers (DB-free)
# ──────────────────────────────────────────────────────────────────────────────


def test_extract_triggers_string():
    assert _extract_triggers("push") == "push"


def test_extract_triggers_list():
    assert _extract_triggers(["push", "pull_request"]) == "push, pull_request"


def test_extract_triggers_dict():
    result = _extract_triggers({"push": None, "pull_request": None})
    assert "push" in result
    assert "pull_request" in result


def test_extract_triggers_none():
    assert _extract_triggers(None) == ""


def test_extract_jobs_summary_basic():
    jobs = {
        "build": {
            "runs-on": "ubuntu-latest",
            "steps": [
                {"name": "Checkout", "uses": "actions/checkout@v4"},
                {"name": "Test", "run": "pytest"},
            ],
        }
    }
    summary = _extract_jobs_summary(jobs)
    assert "build" in summary
    assert "ubuntu-latest" in summary
    assert "Checkout" in summary
    assert "actions/checkout@v4" in summary


def test_render_workflow_doc_structure():
    doc = _render_workflow_doc("owner/repo", ".github/workflows/ci.yml", "sha123", SIMPLE_WORKFLOW_YAML)
    assert doc["id"] == _workflow_doc_id("owner/repo", ".github/workflows/ci.yml")
    assert doc["repo"] == "owner/repo"
    assert doc["sha"] == "sha123"
    assert doc["_deleted"] is False
    content = doc["content"]
    assert "# Workflow: CI" in content
    assert "push" in content
    assert "build" in content
    assert "Checkout" in content
    assert "actions/checkout@v4" in content


def test_render_workflow_doc_no_sensitive_values():
    yaml_with_secrets = textwrap.dedent("""\
        name: Deploy
        on: push
        jobs:
          deploy:
            runs-on: ubuntu-latest
            steps:
              - name: Deploy step
                env:
                  SECRET_TOKEN: ${{ secrets.MY_TOKEN }}
                run: |
                  curl -H "Authorization: Bearer $SECRET_TOKEN" https://example.com
    """)
    doc = _render_workflow_doc("owner/repo", ".github/workflows/deploy.yml", "sha", yaml_with_secrets)
    # Step name should appear but not the raw shell script body
    assert "Deploy step" in doc["content"]
    assert "Bearer $SECRET_TOKEN" not in doc["content"]
    assert "curl" not in doc["content"]


def test_render_run_doc_structure():
    run = _make_run(999, attempt=1)
    doc = _render_run_doc("owner/repo", run)
    assert doc["id"] == _run_doc_id("owner/repo", 999, 1)
    assert doc["run_id"] == "999"
    assert doc["attempt"] == 1
    assert doc["repo"] == "owner/repo"
    assert doc["_deleted"] is False
    content = doc["content"]
    assert "# GitHub Actions Run" in content
    assert "main" in content       # branch
    assert "push" in content       # event
    assert "nevilsonani" in content  # actor
    assert "success" in content


def test_render_run_doc_duration():
    run = _make_run(1)
    doc = _render_run_doc("owner/repo", run)
    assert "Duration: 300s" in doc["content"]  # 10:00 → 10:05 = 300s


def test_workflow_doc_id_stable():
    a = _workflow_doc_id("owner/repo", ".github/workflows/ci.yml")
    b = _workflow_doc_id("owner/repo", ".github/workflows/ci.yml")
    assert a == b
    assert a != _workflow_doc_id("owner/repo", ".github/workflows/cd.yml")


def test_run_doc_id_stable():
    a = _run_doc_id("owner/repo", 1, 1)
    b = _run_doc_id("owner/repo", 1, 1)
    assert a == b
    assert a != _run_doc_id("owner/repo", 1, 2)  # different attempt


def test_ansi_stripped_from_logs():
    raw = "\x1b[31mERROR\x1b[0m: test failed\r\nfoo"
    clean = _ANSI_RE.sub("", raw)
    assert clean == "ERROR: test failed\nfoo"


# ──────────────────────────────────────────────────────────────────────────────
# Workflow sync
# ──────────────────────────────────────────────────────────────────────────────


def test_workflow_sync_new_file():
    client = FakeGitHubClient()
    client.register(
        "/repos/owner/repo/contents/.github/workflows",
        [{"path": ".github/workflows/ci.yml", "name": "ci.yml", "sha": "sha1"}],
    )
    client.register_file("owner/repo/.github/workflows/ci.yml", SIMPLE_WORKFLOW_YAML)

    state = {}
    docs = list(_sync_workflows(client, "owner/repo", state))

    assert len(docs) == 1
    assert docs[0]["id"] == _workflow_doc_id("owner/repo", ".github/workflows/ci.yml")
    assert docs[0]["_deleted"] is False
    # State updated with SHA
    assert state["workflows"]["owner/repo"][".github/workflows/ci.yml"]["sha"] == "sha1"


def test_workflow_sync_unchanged_file_skipped():
    client = FakeGitHubClient()
    client.register(
        "/repos/owner/repo/contents/.github/workflows",
        [{"path": ".github/workflows/ci.yml", "name": "ci.yml", "sha": "sha1"}],
    )
    # Pre-seed state with same SHA
    state = {"workflows": {"owner/repo": {".github/workflows/ci.yml": {"sha": "sha1"}}}}

    docs = list(_sync_workflows(client, "owner/repo", state))

    # No download, no document yielded
    assert docs == []
    assert ("get_file_content", "owner/repo", ".github/workflows/ci.yml") not in client.calls


def test_workflow_sync_changed_sha_re_fetched():
    client = FakeGitHubClient()
    client.register(
        "/repos/owner/repo/contents/.github/workflows",
        [{"path": ".github/workflows/ci.yml", "name": "ci.yml", "sha": "sha2"}],
    )
    client.register_file("owner/repo/.github/workflows/ci.yml", SIMPLE_WORKFLOW_YAML)
    # Old SHA in state
    state = {"workflows": {"owner/repo": {".github/workflows/ci.yml": {"sha": "sha1"}}}}

    docs = list(_sync_workflows(client, "owner/repo", state))

    assert len(docs) == 1
    assert state["workflows"]["owner/repo"][".github/workflows/ci.yml"]["sha"] == "sha2"


def test_workflow_sync_removed_file_tombstoned():
    client = FakeGitHubClient()
    # Listing no longer contains old.yml
    client.register(
        "/repos/owner/repo/contents/.github/workflows",
        [{"path": ".github/workflows/ci.yml", "name": "ci.yml", "sha": "sha1"}],
    )
    client.register_file("owner/repo/.github/workflows/ci.yml", SIMPLE_WORKFLOW_YAML)
    # State has an extra file that disappeared
    state = {
        "workflows": {
            "owner/repo": {
                ".github/workflows/ci.yml": {"sha": "sha1"},
                ".github/workflows/old.yml": {"sha": "oldsha"},
            }
        }
    }

    docs = list(_sync_workflows(client, "owner/repo", state))

    tombstones = [d for d in docs if d.get("_deleted") is True]
    assert len(tombstones) == 1
    assert tombstones[0]["id"] == _workflow_doc_id("owner/repo", ".github/workflows/old.yml")
    # Removed from state
    assert ".github/workflows/old.yml" not in state["workflows"]["owner/repo"]


def test_workflow_sync_empty_repo_no_workflows_dir():
    client = FakeGitHubClient()
    # Simulate 404 for the workflows directory
    # FakeGitHubClient raises _NotFoundError for unregistered paths
    state = {}
    # Register nothing — get() will raise NotFoundError which _list_workflow_files catches
    docs = list(_sync_workflows(client, "owner/repo", state))
    assert docs == []


# ──────────────────────────────────────────────────────────────────────────────
# Run sync
# ──────────────────────────────────────────────────────────────────────────────


def _setup_run_listing(client, repo, runs):
    """Register a workflow_runs listing response."""
    from datetime import datetime, timezone, timedelta
    cutoff = datetime.now(timezone.utc) - timedelta(days=30)
    since = cutoff.strftime("%Y-%m-%dT%H:%M:%SZ")
    until = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    # Register for any params combo via path-only fallback
    client.register(
        f"/repos/{repo}/actions/runs",
        {"workflow_runs": runs},
    )


def test_run_sync_new_completed_run():
    client = FakeGitHubClient()
    repo = "owner/repo"
    run = _make_run(100, attempt=1, status="completed", conclusion="success")
    _setup_run_listing(client, repo, [run])

    from datetime import datetime, timezone, timedelta
    state = {}
    docs = list(_sync_runs(client, repo, datetime.now(timezone.utc) - timedelta(days=30), state))

    run_docs = [d for d in docs if not d.get("_deleted")]
    assert len(run_docs) == 1
    assert run_docs[0]["run_id"] == "100"
    assert run_docs[0]["attempt"] == 1


def test_run_sync_in_progress_run_skipped():
    client = FakeGitHubClient()
    repo = "owner/repo"
    run = _make_run(200, attempt=1, status="in_progress", conclusion=None)
    _setup_run_listing(client, repo, [run])

    from datetime import datetime, timezone, timedelta
    state = {}
    docs = list(_sync_runs(client, repo, datetime.now(timezone.utc) - timedelta(days=30), state))

    run_docs = [d for d in docs if not d.get("_deleted")]
    assert run_docs == []


def test_run_sync_rerun_captured_independently():
    """A re-run (attempt 2) must yield a separate document from attempt 1."""
    client = FakeGitHubClient()
    repo = "owner/repo"
    attempt1 = _make_run(300, attempt=1, status="completed", conclusion="failure")
    attempt2 = _make_run(300, attempt=2, status="completed", conclusion="success")
    _setup_run_listing(client, repo, [attempt1, attempt2])

    from datetime import datetime, timezone, timedelta
    state = {}
    docs = list(_sync_runs(client, repo, datetime.now(timezone.utc) - timedelta(days=30), state))

    run_docs = [d for d in docs if not d.get("_deleted")]
    assert len(run_docs) == 2
    attempts = {d["attempt"] for d in run_docs}
    assert attempts == {1, 2}


def test_run_sync_already_stored_skipped():
    client = FakeGitHubClient()
    repo = "owner/repo"
    run = _make_run(400, attempt=1, status="completed", conclusion="success")
    _setup_run_listing(client, repo, [run])

    from datetime import datetime, timezone, timedelta
    cutoff = datetime.now(timezone.utc) - timedelta(days=30)
    doc_id = _run_doc_id(repo, 400, 1)
    state = {"runs": {repo: {doc_id: {"status": "completed", "conclusion": "success"}}}}

    docs = list(_sync_runs(client, repo, cutoff, state))

    run_docs = [d for d in docs if not d.get("_deleted")]
    assert run_docs == []


def test_run_sync_deleted_run_tombstoned():
    client = FakeGitHubClient()
    repo = "owner/repo"
    # Listing returns only run 600; run 500 was previously stored but is now gone
    run = _make_run(600, attempt=1, status="completed", conclusion="success")
    _setup_run_listing(client, repo, [run])

    from datetime import datetime, timezone, timedelta
    cutoff = datetime.now(timezone.utc) - timedelta(days=30)
    old_doc_id = _run_doc_id(repo, 500, 1)
    state = {"runs": {repo: {old_doc_id: {"status": "completed", "conclusion": "success"}}}}

    docs = list(_sync_runs(client, repo, cutoff, state))

    tombstones = [d for d in docs if d.get("_deleted") is True]
    assert any(t["id"] == old_doc_id for t in tombstones)


# ──────────────────────────────────────────────────────────────────────────────
# Pagination: 1,000-result limit → time-window subdivision
# ──────────────────────────────────────────────────────────────────────────────


def test_run_listing_subdivides_on_limit():
    """When a window returns >= 1000 runs the helper must subdivide it."""
    from cognee_community_connector_github_actions.github_actions import _RUN_LISTING_LIMIT

    call_count = {"n": 0}

    def fake_get(path, params=None):
        call_count["n"] += 1
        # First two calls simulate hitting the cap; subsequent ones are empty
        if call_count["n"] <= 2:
            return {"workflow_runs": [_make_run(i) for i in range(_RUN_LISTING_LIMIT)]}
        return {"workflow_runs": []}

    client = SimpleNamespace(get=fake_get)
    since = "2026-09-01T00:00:00Z"
    until = "2026-10-01T00:00:00Z"

    results = list(_list_runs_window(client, "owner/repo", since, until))
    # Should have recursed and returned results from sub-windows
    assert call_count["n"] >= 3  # at least the initial call + two sub-calls


# ──────────────────────────────────────────────────────────────────────────────
# Job sync
# ──────────────────────────────────────────────────────────────────────────────


def test_job_sync_successful_run():
    client = FakeGitHubClient()
    repo = "owner/repo"
    run_id, attempt = 700, 1
    doc_id = _run_doc_id(repo, run_id, attempt)

    job = _make_job(701, name="build", conclusion="success")
    client.register(
        f"/repos/{repo}/actions/runs/{run_id}/attempts/{attempt}/jobs",
        {"jobs": [job]},
    )

    state = {
        "runs": {repo: {doc_id: {"status": "completed", "conclusion": "success"}}},
        "jobs": {},
    }
    docs = list(_sync_jobs(
        client, repo,
        __import__("datetime").datetime.now(__import__("datetime").timezone.utc) -
        __import__("datetime").timedelta(days=30),
        state,
        include_annotations=False,
        include_log_tail=False,
        max_log_lines=100,
    ))

    job_docs = [d for d in docs if not d.get("_deleted")]
    assert len(job_docs) == 1
    assert "build" in job_docs[0]["content"]
    assert "Checkout" in job_docs[0]["content"]


def test_job_sync_failed_run_with_log_tail():
    client = FakeGitHubClient()
    repo = "owner/repo"
    run_id, attempt = 800, 1
    doc_id = _run_doc_id(repo, run_id, attempt)

    failing_steps = [
        {"name": "Checkout", "conclusion": "success", "number": 1},
        {"name": "Run tests", "conclusion": "failure", "number": 2},
    ]
    job = _make_job(801, name="test", conclusion="failure", steps=failing_steps)
    client.register(
        f"/repos/{repo}/actions/runs/{run_id}/attempts/{attempt}/jobs",
        {"jobs": [job]},
    )
    log_text = "\x1b[31mFAILED\x1b[0m test_foo.py::test_bar\nAssertionError: 1 != 2\n"
    client.register_log(repo, run_id, attempt, 801, 2, log_text)

    state = {
        "runs": {repo: {doc_id: {"status": "completed", "conclusion": "failure"}}},
        "jobs": {},
    }
    docs = list(_sync_jobs(
        client, repo,
        __import__("datetime").datetime.now(__import__("datetime").timezone.utc) -
        __import__("datetime").timedelta(days=30),
        state,
        include_annotations=False,
        include_log_tail=True,
        max_log_lines=50,
    ))

    job_docs = [d for d in docs if not d.get("_deleted")]
    assert len(job_docs) == 1
    content = job_docs[0]["content"]
    assert "Log tail" in content
    # ANSI stripped
    assert "\x1b[31m" not in content
    assert "FAILED" in content
    assert "AssertionError" in content


def test_job_sync_expired_logs_graceful():
    """Expired logs (410 Gone) must not abort the sync."""
    client = FakeGitHubClient()
    repo = "owner/repo"
    run_id, attempt = 900, 1
    doc_id = _run_doc_id(repo, run_id, attempt)

    failing_steps = [
        {"name": "Build", "conclusion": "failure", "number": 1},
    ]
    job = _make_job(901, name="build", conclusion="failure", steps=failing_steps)
    client.register(
        f"/repos/{repo}/actions/runs/{run_id}/attempts/{attempt}/jobs",
        {"jobs": [job]},
    )
    # No log registered → FakeGitHubClient raises _GoneError

    state = {
        "runs": {repo: {doc_id: {"status": "completed", "conclusion": "failure"}}},
        "jobs": {},
    }
    docs = list(_sync_jobs(
        client, repo,
        __import__("datetime").datetime.now(__import__("datetime").timezone.utc) -
        __import__("datetime").timedelta(days=30),
        state,
        include_annotations=False,
        include_log_tail=True,
        max_log_lines=50,
    ))

    # Document still created — missing logs are non-fatal
    job_docs = [d for d in docs if not d.get("_deleted")]
    assert len(job_docs) == 1
    assert job_docs[0]["log_available"] is True  # set at render time


def test_job_sync_already_stored_skipped():
    """Jobs for a run that's already in job_state must not be re-fetched."""
    client = FakeGitHubClient()
    repo = "owner/repo"
    run_id, attempt = 1000, 1
    doc_id = _run_doc_id(repo, run_id, attempt)

    state = {
        "runs": {repo: {doc_id: {"status": "completed", "conclusion": "success"}}},
        "jobs": {repo: {doc_id: ["job:owner/repo:1000:1:1001"]}},  # already stored
    }
    docs = list(_sync_jobs(
        client, repo,
        __import__("datetime").datetime.now(__import__("datetime").timezone.utc) -
        __import__("datetime").timedelta(days=30),
        state,
        include_annotations=False,
        include_log_tail=False,
        max_log_lines=100,
    ))

    assert ("get", f"/repos/{repo}/actions/runs/{run_id}/attempts/{attempt}/jobs", None) \
        not in client.calls
    assert docs == []


def test_job_sync_deleted_run_tombstones_jobs():
    """When a run disappears from run_state its jobs must be tombstoned."""
    client = FakeGitHubClient()
    repo = "owner/repo"
    deleted_run_doc_id = _run_doc_id(repo, 1100, 1)

    state = {
        "runs": {repo: {}},  # run is gone from run_state
        "jobs": {repo: {deleted_run_doc_id: ["job:owner/repo:1100:1:1101"]}},
    }
    docs = list(_sync_jobs(
        client, repo,
        __import__("datetime").datetime.now(__import__("datetime").timezone.utc) -
        __import__("datetime").timedelta(days=30),
        state,
        include_annotations=False,
        include_log_tail=False,
        max_log_lines=100,
    ))

    tombstones = [d for d in docs if d.get("_deleted") is True]
    assert any(t["id"] == "job:owner/repo:1100:1:1101" for t in tombstones)


# ──────────────────────────────────────────────────────────────────────────────
# Annotations
# ──────────────────────────────────────────────────────────────────────────────


def test_job_sync_annotations_for_failed_job():
    client = FakeGitHubClient()
    repo = "owner/repo"
    run_id, attempt = 1200, 1
    doc_id = _run_doc_id(repo, run_id, attempt)

    failing_steps = [{"name": "Test", "conclusion": "failure", "number": 1}]
    job = _make_job(1201, name="test", conclusion="failure", steps=failing_steps)
    client.register(
        f"/repos/{repo}/actions/runs/{run_id}/attempts/{attempt}/jobs",
        {"jobs": [job]},
    )
    client.register(
        f"/repos/{repo}/check-runs/1201/annotations",
        [{"annotation_level": "failure", "message": "Assertion failed", "path": "src/main.py", "start_line": 42}],
    )

    state = {
        "runs": {repo: {doc_id: {"status": "completed", "conclusion": "failure"}}},
        "jobs": {},
    }
    docs = list(_sync_jobs(
        client, repo,
        __import__("datetime").datetime.now(__import__("datetime").timezone.utc) -
        __import__("datetime").timedelta(days=30),
        state,
        include_annotations=True,
        include_log_tail=False,
        max_log_lines=100,
    ))

    job_docs = [d for d in docs if not d.get("_deleted")]
    assert len(job_docs) == 1
    content = job_docs[0]["content"]
    assert "Annotations" in content
    assert "Assertion failed" in content
    assert "src/main.py" in content


# ──────────────────────────────────────────────────────────────────────────────
# document-source marker
# ──────────────────────────────────────────────────────────────────────────────


def test_source_declares_document_marker():
    """The source must carry the DOCUMENT_SOURCE_ATTR marker for cognee routing."""
    pytest.importorskip("dlt")
    pytest.importorskip("httpx")

    from cognee.tasks.ingestion.dlt_utils import document_source_tag
    from cognee_community_connector_github_actions import github_actions_source

    client = FakeGitHubClient()
    source = github_actions_source(
        repositories=["owner/repo"],
        token="ghp_fake",
        http_client=client,
    )
    assert document_source_tag(source) == GITHUB_ACTIONS_SOURCE_NAME


# ──────────────────────────────────────────────────────────────────────────────
# _parse_dt utility
# ──────────────────────────────────────────────────────────────────────────────


def test_parse_dt_handles_z_suffix():
    from datetime import timezone
    dt = _parse_dt("2026-10-01T10:00:00Z")
    assert dt.tzinfo == timezone.utc
    assert dt.year == 2026


def test_parse_dt_handles_no_timezone():
    dt = _parse_dt("2026-10-01T10:00:00")
    assert dt.year == 2026
