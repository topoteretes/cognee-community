"""Unit tests for the GitHub Actions dlt connector.

All runnable in CI without a live GitHub token — the GitHub REST API is faked
with ``httpx.MockTransport``:

* DB-free tests for run → row rendering, Link-header pagination, the snapshot
  window, retry/rate-limit handling, and log tails.
* dlt-pipeline tests (temp sqlite destination) covering the acceptance
  criteria: re-runs are reflected on re-sync, runs that leave the window drop
  out of the full-snapshot load (forget-on-delete), and an API failure aborts
  the sync instead of loading a partial snapshot.
"""

from types import SimpleNamespace
from uuid import NAMESPACE_OID, uuid5

import httpx
import pytest

from cognee_community_connector_github_actions import github_actions as gha
from cognee_community_connector_github_actions import github_actions_source

REPO = "acme/widgets"

# ---------------------------------------------------------------------------
# Fixtures / fakes
# ---------------------------------------------------------------------------


def _run(run_id, conclusion="success", run_number=None, attempt=1, name="CI"):
    return {
        "id": run_id,
        "name": name,
        "path": ".github/workflows/ci.yml",
        "run_number": run_number or run_id,
        "run_attempt": attempt,
        "status": "completed",
        "conclusion": conclusion,
        "event": "push",
        "head_branch": "main",
        "head_sha": "abcdef1234567890",
        "head_commit": {"message": "Fix the widget\n\nLonger body."},
        "actor": {"login": "octocat"},
        "run_started_at": "2026-10-01T10:00:00Z",
        "updated_at": "2026-10-01T10:05:00Z",
        "html_url": f"https://github.com/{REPO}/actions/runs/{run_id}",
    }


def _job(job_id, conclusion="success", name="test", failed_step=None):
    steps = [{"name": "Checkout", "conclusion": "success"}]
    if failed_step:
        steps.append({"name": failed_step, "conclusion": "failure"})
    return {"id": job_id, "name": name, "conclusion": conclusion, "steps": steps}


class FakeGitHub:
    """In-memory GitHub Actions API, served through httpx.MockTransport."""

    def __init__(self, runs, jobs=None, logs=None, page_size=100):
        self.runs = runs
        self.jobs = jobs or {}
        self.logs = logs or {}
        self.page_size = page_size
        self.requests: list[httpx.Request] = []
        # Queue of canned responses served before normal routing (errors, 429s).
        self.injected: list[httpx.Response] = []

    def client(self) -> httpx.Client:
        return httpx.Client(base_url="https://api.github.com", transport=httpx.MockTransport(self))

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.injected:
            return self.injected.pop(0)

        path = request.url.path
        if path.endswith("/actions/runs") or ("/workflows/" in path and path.endswith("/runs")):
            return self._page(request, "workflow_runs", self.runs)
        if path.endswith("/jobs"):
            run_id = int(path.split("/")[-2])
            return self._page(request, "jobs", self.jobs.get(run_id, []))
        if path.endswith("/logs"):
            job_id = int(path.split("/")[-2])
            if job_id not in self.logs:
                return httpx.Response(404, json={"message": "Not Found"})
            return httpx.Response(200, text=self.logs[job_id])
        return httpx.Response(404, json={"message": "Not Found"})

    def _page(self, request, key, items):
        page = int(request.url.params.get("page", 1))
        start = (page - 1) * self.page_size
        chunk = items[start : start + self.page_size]
        headers = {}
        if start + self.page_size < len(items):
            next_url = request.url.copy_set_param("page", page + 1)
            headers["link"] = f'<{next_url}>; rel="next"'
        return httpx.Response(200, json={key: chunk}, headers=headers)


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    monkeypatch.setattr(gha.time, "sleep", lambda _seconds: None)


def _rows(fake, **kwargs):
    """Drive the source's resource directly and return its rows."""
    source = github_actions_source(repos=[REPO], client=fake.client(), **kwargs)
    return list(source.resources[gha.GITHUB_ACTIONS_TABLE_NAME])


def _list_runs(fake):
    """List runs without dlt, so API errors surface unwrapped."""
    return list(gha._iter_runs(fake.client(), REPO, None, None, None, None))


# ---------------------------------------------------------------------------
# Argument validation
# ---------------------------------------------------------------------------


def test_requires_repos():
    with pytest.raises(ValueError, match="repository"):
        github_actions_source(repos=[], token="x")


@pytest.mark.parametrize("repo", ["widgets", "acme/", "/widgets", "a/b/c"])
def test_rejects_malformed_repo(repo):
    with pytest.raises(ValueError, match="owner/repo"):
        github_actions_source(repos=[repo], token="x")


def test_rejects_zero_workers():
    with pytest.raises(ValueError, match="max_workers"):
        github_actions_source(repos=[REPO], token="x", max_workers=0)


def test_requires_token(monkeypatch):
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    with pytest.raises(ValueError, match="GITHUB_TOKEN"):
        github_actions_source(repos=[REPO])


def test_token_falls_back_to_env(monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_test")
    github_actions_source(repos=[REPO])  # does not raise; no request is made yet


# ---------------------------------------------------------------------------
# Rendering (DB-free)
# ---------------------------------------------------------------------------


def test_run_to_row_renders_run_and_jobs():
    fake = FakeGitHub(
        runs=[],
        jobs={7: [_job(1, "success", "lint"), _job(2, "failure", "test", "Run pytest")]},
    )
    row = gha._run_to_row(fake.client(), REPO, _run(7, "failure"), False, 50)

    assert row["id"] == f"{REPO}:7"
    assert row["url"] == f"https://github.com/{REPO}/actions/runs/7"
    assert row["title"] == f"CI #7 — failure ({REPO})"
    content = row["content"]
    assert "Conclusion: failure" in content
    assert "Commit: abcdef1 Fix the widget" in content  # first message line only
    assert "Longer body" not in content
    assert "- lint: success" in content
    assert "- test: failure" in content
    assert "  - failed step: Run pytest" in content
    assert "Failure log" not in content  # logs are opt-in
    # Identity/provenance + text only — nothing volatile like status.
    assert set(row) == {"id", "url", "title", "content"}


def _log(*lines):
    """A job log as GitHub serves it: BOM, then timestamp-prefixed lines."""
    stamped = [f"2026-10-01T10:00:0{i % 10}.1234567Z {line}" for i, line in enumerate(lines)]
    return "\ufeff" + "\n".join(stamped)


# Shape of a real failed job log: runner setup, a passing step, then the failed
# step's header group (command echo in ANSI colour, shell, env), its output,
# the error markers, and post-job cleanup.
FAILED_STEP_LOG = _log(
    "Current runner version: '2.337.0'",
    "##[group]Operating System",
    "Ubuntu",
    "##[endgroup]",
    "##[group]Run actions/checkout@v4",
    "with:",
    "##[endgroup]",
    "Syncing repository: acme/widgets",
    "##[group]Run uv run pytest",
    "\x1b[36;1muv run pytest\x1b[0m",
    "shell: /usr/bin/bash -e {0}",
    "env:",
    "  SECRET_FLAG: true",
    "##[endgroup]",
    "collected 3 items",
    "##[group]Captured output",
    "\x1b[31mtests/test_widget.py::test_spin FAILED\x1b[0m",
    "##[endgroup]",
    "",
    "AssertionError: widget did not spin",
    "##[error]AssertionError: widget did not spin",
    "##[error]Process completed with exit code 1.",
    "##[error]Process completed with exit code 1.",
    "Post job cleanup.",
    "Cleaning up orphan processes",
)


def test_summarize_log_keeps_errors_and_failed_step_output():
    errors, output = gha._summarize_log(FAILED_STEP_LOG, 50)

    # Distinct error messages, in order, markers stripped.
    assert errors == ["AssertionError: widget did not spin", "Process completed with exit code 1."]
    # Only the failed step's own output: no runner setup, no earlier steps, no
    # command/env header, no group markers, no ANSI codes, no blank lines.
    assert output == [
        "collected 3 items",
        "tests/test_widget.py::test_spin FAILED",
        "AssertionError: widget did not spin",
    ]


def test_summarize_log_caps_output_lines():
    _, output = gha._summarize_log(FAILED_STEP_LOG, 1)
    assert output == ["AssertionError: widget did not spin"]
    assert gha._summarize_log(FAILED_STEP_LOG, 0)[1] == []


def test_summarize_log_error_with_no_step_output():
    # The real failure seen on bump_cognee.yml: the step only echoes an error.
    log = _log(
        '##[group]Run echo "::error::Set the PR token secret."',
        "\x1b[36;1mexit 1\x1b[0m",
        "##[endgroup]",
        "##[error]Set the PR token secret.",
        "##[error]Process completed with exit code 1.",
    )
    assert gha._summarize_log(log, 50) == (
        ["Set the PR token secret.", "Process completed with exit code 1."],
        [],
    )


def test_summarize_log_without_errors_falls_back_to_tail():
    log = _log("one", "\x1b[1mtwo\x1b[0m", "##[endgroup]", "three")
    assert gha._summarize_log(log, 2) == ([], ["two", "three"])


def test_clean_log_line():
    assert gha._clean_log_line("\ufeff2026-10-01T10:00:00.1Z \x1b[36;1mhello\x1b[0m") == "hello"


def test_failed_job_log_is_rendered():
    fake = FakeGitHub(
        runs=[],
        jobs={7: [_job(1, "success", "lint"), _job(2, "failure", "test")]},
        logs={1: "lint log", 2: FAILED_STEP_LOG},
    )
    content = gha._run_to_row(fake.client(), REPO, _run(7, "failure"), True, 50)["content"]

    assert "## Failure log: test" in content
    assert "Errors:\n- AssertionError: widget did not spin\n" in content
    assert "Output before the error:\n```text\ncollected 3 items\n" in content
    assert "Current runner version" not in content
    assert "SECRET_FLAG" not in content  # step env header is dropped
    assert "\x1b" not in content
    assert "lint log" not in content  # only failed jobs' logs are fetched
    assert not any(r.url.path.endswith("/jobs/1/logs") for r in fake.requests)


def test_expired_log_is_skipped():
    fake = FakeGitHub(runs=[], jobs={7: [_job(2, "failure")]}, logs={})
    content = gha._run_to_row(fake.client(), REPO, _run(7, "failure"), True, 50)["content"]
    assert "- test: failure" in content
    assert "Failure log" not in content


def test_source_declares_document_marker():
    from cognee.tasks.ingestion.dlt_utils import document_source_tag

    source = github_actions_source(repos=[REPO], token="ghp_test")
    assert gha.GITHUB_ACTIONS_SOURCE_NAME == "github_actions"
    assert document_source_tag(source) == "github_actions"


def test_build_document_data_item_tags_source():
    from cognee.tasks.ingestion.resolve_dlt_sources import _build_document_data_item

    fake = FakeGitHub(runs=[], jobs={7: [_job(1)]})
    row_data = gha._run_to_row(fake.client(), REPO, _run(7), False, 50)
    row = SimpleNamespace(row_data=row_data, content_hash="abc123")
    data_id = uuid5(NAMESPACE_OID, row_data["id"])

    item = _build_document_data_item(row, data_id, "github_actions")

    # source="github_actions" (not "dlt") routes the run through normal cognify.
    assert item.external_metadata["source"] == "github_actions"
    assert item.external_metadata["external_id"] == f"{REPO}:7"
    assert item.data.startswith(f"# CI #7 — success ({REPO})")


# ---------------------------------------------------------------------------
# Listing: pagination, filters, snapshot window
# ---------------------------------------------------------------------------


def test_pagination_follows_link_header():
    fake = FakeGitHub(runs=[_run(i) for i in range(1, 6)], page_size=2)
    rows = _rows(fake, max_runs_per_repo=None)
    assert [r["id"] for r in rows] == [f"{REPO}:{i}" for i in range(1, 6)]


def test_serial_and_concurrent_fetching_give_the_same_rows():
    runs = [_run(i, "failure" if i % 3 == 0 else "success") for i in range(1, 12)]
    jobs = {i: [_job(100 + i, run["conclusion"])] for i, run in enumerate(runs, start=1)}

    serial = _rows(FakeGitHub(runs, jobs, page_size=4), max_runs_per_repo=None, max_workers=1)
    concurrent = _rows(FakeGitHub(runs, jobs, page_size=4), max_runs_per_repo=None, max_workers=8)

    assert concurrent == serial  # same rows, same (listing) order
    assert [r["id"] for r in serial] == [f"{REPO}:{i}" for i in range(1, 12)]


def test_max_runs_per_repo_caps_the_window():
    fake = FakeGitHub(runs=[_run(i) for i in range(1, 6)], page_size=2)
    rows = _rows(fake, max_runs_per_repo=3)
    assert [r["id"] for r in rows] == [f"{REPO}:{i}" for i in range(1, 4)]
    # Stops paging once the window is full (pages 1-2 only).
    run_pages = [r for r in fake.requests if r.url.path.endswith("/actions/runs")]
    assert len(run_pages) == 2


def test_filters_are_sent_to_the_api():
    fake = FakeGitHub(runs=[_run(1)])
    _rows(fake, workflow="ci.yml", branch="main", created_after="2026-09-01")

    listing = fake.requests[0]
    assert listing.url.path == f"/repos/{REPO}/actions/workflows/ci.yml/runs"
    assert listing.url.params["status"] == "completed"
    assert listing.url.params["branch"] == "main"
    assert listing.url.params["created"] == ">=2026-09-01"


# ---------------------------------------------------------------------------
# Error handling: retries, and no partial snapshots
# ---------------------------------------------------------------------------


def test_retries_rate_limit_then_succeeds():
    fake = FakeGitHub(runs=[_run(1)])
    fake.injected = [
        httpx.Response(429, headers={"retry-after": "1"}),
        httpx.Response(403, headers={"x-ratelimit-remaining": "0", "x-ratelimit-reset": "0"}),
        httpx.Response(503),
    ]
    assert [r["id"] for r in _rows(fake)] == [f"{REPO}:1"]


def test_permission_error_is_not_retried():
    fake = FakeGitHub(runs=[_run(1)])
    fake.injected = [httpx.Response(403, json={"message": "Resource not accessible"})]
    with pytest.raises(httpx.HTTPStatusError):
        _list_runs(fake)
    assert len(fake.requests) == 1


def test_gives_up_after_max_retries():
    fake = FakeGitHub(runs=[_run(1)])
    fake.injected = [httpx.Response(502)] * gha._MAX_RETRIES
    with pytest.raises(httpx.HTTPStatusError):
        _list_runs(fake)
    assert len(fake.requests) == gha._MAX_RETRIES


def test_long_rate_limit_reset_fails_fast():
    fake = FakeGitHub(runs=[_run(1)])
    fake.injected = [httpx.Response(429, headers={"retry-after": "3600"})]
    with pytest.raises(httpx.HTTPStatusError):
        _list_runs(fake)
    assert len(fake.requests) == 1


def test_classification_of_403s():
    def resp(status, **headers):
        return httpx.Response(status, headers=headers)

    assert gha._is_rate_limited(resp(429)) is True
    assert gha._is_rate_limited(resp(403, **{"x-ratelimit-remaining": "0"})) is True
    assert gha._is_rate_limited(resp(403, **{"retry-after": "5"})) is True
    assert gha._is_rate_limited(resp(403)) is False
    assert gha._is_rate_limited(resp(404)) is False


# ---------------------------------------------------------------------------
# dlt pipeline: full-snapshot sync + forget-on-delete
# ---------------------------------------------------------------------------


def _pipeline(dlt, tmp_path):
    """A dlt pipeline into a temp sqlite destination (same one on every call)."""
    db_path = (tmp_path / "gha.db").as_posix()
    return dlt.pipeline(
        pipeline_name="github_actions_test",
        destination=dlt.destinations.sqlalchemy(f"sqlite:///{db_path}"),
        dataset_name="github_actions_ds",
        pipelines_dir=str(tmp_path / "state"),
    )


def _run_sync(dlt, tmp_path, fake, **kwargs):
    """Run github_actions_source through the temp pipeline."""
    pipeline = _pipeline(dlt, tmp_path)
    pipeline.run(github_actions_source(repos=[REPO], client=fake.client(), **kwargs))
    return pipeline


def _read_runs(pipeline):
    """Return {id: row-dict} for the github_actions_runs table."""
    with (
        pipeline.sql_client() as client,
        client.execute_query(
            f"SELECT id, title, content FROM {gha.GITHUB_ACTIONS_TABLE_NAME}"
        ) as cursor,
    ):
        rows = cursor.fetchall()
    return {row[0]: {"id": row[0], "title": row[1], "content": row[2]} for row in rows}


@pytest.fixture
def dlt_mod():
    return pytest.importorskip("dlt")


def test_first_sync_loads_runs(dlt_mod, tmp_path):
    fake = FakeGitHub(runs=[_run(1), _run(2, "failure")], jobs={2: [_job(9, "failure")]})
    rows = _read_runs(_run_sync(dlt_mod, tmp_path, fake))

    assert set(rows) == {f"{REPO}:1", f"{REPO}:2"}
    assert "- test: failure" in rows[f"{REPO}:2"]["content"]


def test_rerun_is_reflected_on_resync(dlt_mod, tmp_path):
    _run_sync(dlt_mod, tmp_path, FakeGitHub(runs=[_run(1, "failure")]))

    # Re-running the workflow bumps the attempt and changes the conclusion.
    fake = FakeGitHub(runs=[_run(1, "success", attempt=2)])
    rows = _read_runs(_run_sync(dlt_mod, tmp_path, fake))

    assert "Conclusion: success" in rows[f"{REPO}:1"]["content"]
    assert "attempt 2" in rows[f"{REPO}:1"]["content"]


def test_run_leaving_the_window_is_removed_on_resync(dlt_mod, tmp_path):
    _run_sync(dlt_mod, tmp_path, FakeGitHub(runs=[_run(2), _run(1)]), max_runs_per_repo=2)

    # A newer run arrives; run 1 is pushed out of the 2-run window (the same
    # happens when GitHub's retention deletes it). Absent from the replace
    # load → falls out of staging → orphan cleanup forgets it downstream.
    fake = FakeGitHub(runs=[_run(3), _run(2), _run(1)])
    rows = _read_runs(_run_sync(dlt_mod, tmp_path, fake, max_runs_per_repo=2))

    assert set(rows) == {f"{REPO}:3", f"{REPO}:2"}


def test_api_failure_aborts_sync_and_keeps_staging(dlt_mod, tmp_path):
    from dlt.pipeline.exceptions import PipelineStepFailed

    _run_sync(dlt_mod, tmp_path, FakeGitHub(runs=[_run(1), _run(2)]))

    # Jobs listing for run 2 fails persistently: the run must abort rather than
    # load a partial snapshot that would make cognee forget run 2.
    fake = FakeGitHub(runs=[_run(1), _run(2)])
    fake.injected = [httpx.Response(200, json={"workflow_runs": [_run(1), _run(2)]})]
    fake.injected += [httpx.Response(200, json={"jobs": []})]
    fake.injected += [httpx.Response(500)] * gha._MAX_RETRIES
    with pytest.raises(PipelineStepFailed):
        _run_sync(dlt_mod, tmp_path, fake)

    assert set(_read_runs(_pipeline(dlt_mod, tmp_path))) == {f"{REPO}:1", f"{REPO}:2"}
