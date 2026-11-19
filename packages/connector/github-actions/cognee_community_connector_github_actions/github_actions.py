"""DLT source for GitHub Actions (incremental sync + forget-on-delete).

Fetches GitHub Actions workflow definitions, run attempts, and job documents
from one or more repositories, then yields them as a dlt resource for cognee's
ingestion pipeline.

Unlike the relational dlt path (SQL/CSV), documents are ingested in *document
mode*: the source declares ``cognee_document_source = "github_actions"``, so
``resolve_dlt_sources`` tags each row ``external_metadata["source"] = "github_actions"``
(not ``"dlt"``). ``is_dlt_sourced`` therefore returns False and each document
flows through the standard cognify entity-extraction pipeline — the right
treatment for prose — instead of the deterministic dlt-row schema-context path.

Sync strategy
-------------
* **Workflow definitions** — full snapshot of ``.github/workflows/`` using blob
  SHA/ETag caching.  Unchanged files are never re-downloaded.  Removed files
  receive tombstones.

* **Run attempts** — incremental merge.  Each sync sweeps a configurable
  lookback window, comparing ``(run_id, run_attempt, status, conclusion)``
  against saved state.  In-progress attempts are left alone; completed ones are
  fetched and stored exactly once.  Re-runs (same ``run_id``, higher
  ``run_attempt``) are captured independently so flaky history stays queryable.

* **Jobs** — fetched per completed attempt via the attempt-specific endpoint.

Forget-on-delete
----------------
Runs that are explicitly deleted upstream or cross the lookback boundary are
tombstoned (``_deleted=True``) so cognee's ``orphan_cleanup`` removes them.
A partial API response or failed pagination never triggers tombstones — the
sweep must complete fully before reconciliation runs.

Rate limits
-----------
Conditional requests (ETags / If-None-Match) avoid burning the primary limit for
unchanged workflow files.  ``Retry-After`` / ``X-RateLimit-Reset`` are honoured,
with bounded exponential backoff for transient failures.
"""

import hashlib
import os
import re
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Iterator

from cognee.shared.logging_utils import get_logger
from cognee.tasks.ingestion.dlt_utils import DOCUMENT_SOURCE_ATTR

logger = get_logger("github_actions_connector")

# ──────────────────────────────────────────────────────────────────────────────
# Constants
# ──────────────────────────────────────────────────────────────────────────────

GITHUB_ACTIONS_SOURCE_NAME = "github_actions"

# dlt resource / staging-table names
_TABLE_WORKFLOWS = "github_actions_workflows"
_TABLE_RUNS = "github_actions_runs"
_TABLE_JOBS = "github_actions_jobs"

_GITHUB_API_BASE = "https://api.github.com"
_ACCEPT_V3 = "application/vnd.github+json"
_API_VERSION_HEADER = "2022-11-28"

_MAX_RETRIES = 6
_PER_PAGE = 100
# GitHub caps filtered run searches at 1 000 results; split windows when hit.
_RUN_LISTING_LIMIT = 1000
# Lines of log tail to capture for failing steps (to keep docs manageable).
_DEFAULT_MAX_LOG_LINES = 100

# ANSI / VT100 control-sequence stripper
_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]|\r")

_EXTRA_HINT = (
    "The GitHub Actions connector requires the 'github_actions' extra:\n"
    "  pip install cognee-community-connector-github-actions\n"
    "(provides dlt and httpx)."
)


# ──────────────────────────────────────────────────────────────────────────────
# Public entry-point
# ──────────────────────────────────────────────────────────────────────────────


def github_actions_source(
    repositories: list[str],
    token: str | None = None,
    lookback_days: int = 30,
    include_annotations: bool = True,
    include_log_tail: bool = True,
    max_log_lines: int = _DEFAULT_MAX_LOG_LINES,
    http_client: Any = None,
):
    """Create a dlt source that yields GitHub Actions documents for cognee.

    Args:
        repositories: List of ``"owner/repo"`` strings to ingest.
        token: GitHub personal access token (classic or fine-grained).
            Falls back to the ``GITHUB_TOKEN`` environment variable.
            Required fine-grained permissions per repository:
            * Actions: Read
            * Contents: Read  (for .github/workflows/)
            * Checks: Read    (only when ``include_annotations=True``)
        lookback_days: How many days of run history to sync and retain.
            Runs older than this are tombstoned on the next sync.
        include_annotations: Fetch check-run annotations for failed jobs.
            Requires the Checks: Read permission.
        include_log_tail: Fetch the tail of failing-step logs.
        max_log_lines: Maximum log lines to capture per failing step.
        http_client: Injectable ``_GitHubClient`` (test seam); when omitted,
            one is built from the token above.

    Returns:
        A dlt source suitable for ``cognee.remember(...)``.
    """
    try:
        import dlt  # noqa: F401 – presence check only here; used inside resources
    except ImportError as exc:
        raise ImportError(_EXTRA_HINT) from exc

    resolved_token = token or os.environ.get("GITHUB_TOKEN")
    if resolved_token is None:
        raise ValueError(
            "GitHub token required: pass token= or set GITHUB_TOKEN.\n"
            "Fine-grained PAT permissions needed:\n"
            "  Actions: Read, Contents: Read"
            + (", Checks: Read" if include_annotations else "")
        )

    client = http_client or _GitHubClient(resolved_token)

    return _build_source(
        client=client,
        repositories=repositories,
        lookback_days=lookback_days,
        include_annotations=include_annotations,
        include_log_tail=include_log_tail,
        max_log_lines=max_log_lines,
    )


# ──────────────────────────────────────────────────────────────────────────────
# DLT source / resource definitions
# ──────────────────────────────────────────────────────────────────────────────


def _build_source(
    *,
    client: "Any",
    repositories: list[str],
    lookback_days: int,
    include_annotations: bool,
    include_log_tail: bool,
    max_log_lines: int,
):
    import dlt

    @dlt.resource(
        name=_TABLE_WORKFLOWS,
        primary_key="id",
        write_disposition={
            "disposition": "merge",
            "strategy": "upsert",
        },
        columns={"_deleted": {"data_type": "bool", "hard_delete": True}},
    )
    def workflow_definitions(dlt_state=dlt.state()):
        """Yield workflow definition documents — one per .github/workflows/ file."""
        for repo in repositories:
            yield from _sync_workflows(client, repo, dlt_state)

    @dlt.resource(
        name=_TABLE_RUNS,
        primary_key="id",
        write_disposition={
            "disposition": "merge",
            "strategy": "upsert",
        },
        columns={"_deleted": {"data_type": "bool", "hard_delete": True}},
    )
    def run_attempts(dlt_state=dlt.state()):
        """Yield run-attempt documents — one per (run_id, attempt_number) pair."""
        cutoff = datetime.now(timezone.utc) - timedelta(days=lookback_days)
        for repo in repositories:
            yield from _sync_runs(client, repo, cutoff, dlt_state)

    @dlt.resource(
        name=_TABLE_JOBS,
        primary_key="id",
        write_disposition={
            "disposition": "merge",
            "strategy": "upsert",
        },
        columns={"_deleted": {"data_type": "bool", "hard_delete": True}},
    )
    def jobs(dlt_state=dlt.state()):
        """Yield job documents — one per job in each newly-completed run attempt."""
        cutoff = datetime.now(timezone.utc) - timedelta(days=lookback_days)
        for repo in repositories:
            yield from _sync_jobs(
                client,
                repo,
                cutoff,
                dlt_state,
                include_annotations=include_annotations,
                include_log_tail=include_log_tail,
                max_log_lines=max_log_lines,
            )

    @dlt.source(name=GITHUB_ACTIONS_SOURCE_NAME)
    def _github_actions():
        return workflow_definitions, run_attempts, jobs

    source = _github_actions()
    setattr(source, DOCUMENT_SOURCE_ATTR, GITHUB_ACTIONS_SOURCE_NAME)
    return source


# ──────────────────────────────────────────────────────────────────────────────
# Workflow-definition sync
# ──────────────────────────────────────────────────────────────────────────────


def _sync_workflows(
    client: "Any", repo: str, state: dict
) -> Iterator[dict]:
    """Yield workflow definition rows, caching by blob SHA to skip unchanged files."""
    wf_state: dict = state.setdefault("workflows", {}).setdefault(repo, {})

    try:
        current_files = _list_workflow_files(client, repo)
    except Exception as exc:
        logger.error("GitHub Actions: failed to list workflows for %s: %s", repo, exc)
        raise

    current_paths: set[str] = set()
    for item in current_files:
        path: str = item["path"]
        sha: str = item["sha"]
        current_paths.add(path)

        cached = wf_state.get(path, {})
        if cached.get("sha") == sha:
            # Blob unchanged — no download needed.
            continue

        try:
            raw_yaml = client.get_file_content(repo, path)
        except Exception as exc:
            logger.error("GitHub Actions: failed to fetch %s/%s: %s", repo, path, exc)
            raise

        doc = _render_workflow_doc(repo, path, sha, raw_yaml)
        wf_state[path] = {"sha": sha}
        logger.debug("GitHub Actions: synced workflow %s/%s", repo, path)
        yield doc

    # Tombstone workflows that were removed from .github/workflows/.
    for old_path in list(wf_state.keys()):
        if old_path not in current_paths:
            doc_id = _workflow_doc_id(repo, old_path)
            logger.info("GitHub Actions: workflow removed %s/%s — tombstoning", repo, old_path)
            yield {"id": doc_id, "_deleted": True}
            del wf_state[old_path]


def _list_workflow_files(client: "Any", repo: str) -> list[dict]:
    """Return [{path, sha}] for every YAML file under .github/workflows/."""
    try:
        items = client.get(f"/repos/{repo}/contents/.github/workflows")
    except _NotFoundError:
        return []
    return [
        {"path": item["path"], "sha": item["sha"]}
        for item in items
        if isinstance(item, dict) and item.get("name", "").endswith((".yml", ".yaml"))
    ]


def _render_workflow_doc(repo: str, path: str, sha: str, raw_yaml: str) -> dict:
    """Render a workflow YAML into a cognee-digestible document row.

    Only structural metadata is included (triggers, job names, runner labels,
    step names, uses: references). Raw env/secrets/shell-script bodies are
    excluded so sensitive values don't end up in the knowledge graph.
    """
    try:
        import yaml  # pyyaml, listed as optional dep

        wf = yaml.safe_load(raw_yaml) or {}
    except Exception:
        wf = {}

    name = wf.get("name") or path
    on_triggers = _extract_triggers(wf.get("on") or wf.get(True))  # 'on' parses as True in YAML
    jobs_summary = _extract_jobs_summary(wf.get("jobs") or {})

    content_lines = [
        f"# Workflow: {name}",
        f"Repository: {repo}",
        f"File: {path}",
        "",
        "## Triggers",
        on_triggers or "(none detected)",
        "",
        "## Jobs",
        jobs_summary or "(no jobs detected)",
    ]
    content = "\n".join(content_lines)
    doc_id = _workflow_doc_id(repo, path)

    return {
        "id": doc_id,
        "repo": repo,
        "path": path,
        "name": name,
        "sha": sha,
        "content": content,
        "_deleted": False,
    }


def _extract_triggers(on_value) -> str:
    if on_value is None:
        return ""
    if isinstance(on_value, str):
        return on_value
    if isinstance(on_value, list):
        return ", ".join(str(v) for v in on_value)
    if isinstance(on_value, dict):
        return ", ".join(str(k) for k in on_value.keys())
    return str(on_value)


def _extract_jobs_summary(jobs: dict) -> str:
    lines = []
    for job_name, job_def in jobs.items():
        if not isinstance(job_def, dict):
            continue
        runs_on = job_def.get("runs-on", "")
        needs = job_def.get("needs", "")
        steps = job_def.get("steps") or []
        step_names = []
        uses_refs = []
        for step in steps:
            if not isinstance(step, dict):
                continue
            if step.get("name"):
                step_names.append(step["name"])
            if step.get("uses"):
                uses_refs.append(step["uses"])
        line = f"- {job_name} (runs-on: {runs_on})"
        if needs:
            needs_str = needs if isinstance(needs, str) else ", ".join(needs)
            line += f", needs: {needs_str}"
        if step_names:
            line += f"\n  Steps: {', '.join(step_names[:10])}"
        if uses_refs:
            line += f"\n  Uses: {', '.join(uses_refs[:10])}"
        lines.append(line)
    return "\n".join(lines)


def _workflow_doc_id(repo: str, path: str) -> str:
    return f"workflow:{repo}:{path}"


# ──────────────────────────────────────────────────────────────────────────────
# Run-attempt sync
# ──────────────────────────────────────────────────────────────────────────────


def _sync_runs(
    client: "Any",
    repo: str,
    cutoff: datetime,
    state: dict,
) -> Iterator[dict]:
    """Yield run-attempt rows within the lookback window, merging by stable id."""
    run_state: dict = state.setdefault("runs", {}).setdefault(repo, {})

    # Sweep all runs in the window, respecting the 1,000-result limit.
    try:
        current_runs = _list_all_runs(client, repo, cutoff)
    except Exception as exc:
        logger.error("GitHub Actions: failed to list runs for %s: %s", repo, exc)
        raise

    current_ids: set[str] = set()
    for run in current_runs:
        run_id = run["id"]
        attempt = run.get("run_attempt", 1)
        doc_id = _run_doc_id(repo, run_id, attempt)
        current_ids.add(doc_id)

        cached = run_state.get(doc_id, {})
        status = run.get("status", "")
        conclusion = run.get("conclusion") or ""

        # Skip in-progress / queued attempts — they'll be captured once done.
        if status not in ("completed",):
            continue

        # Skip already-stored completed attempts (immutable once done).
        if cached.get("status") == "completed" and cached.get("conclusion") == conclusion:
            continue

        doc = _render_run_doc(repo, run)
        run_state[doc_id] = {"status": status, "conclusion": conclusion}
        logger.debug("GitHub Actions: synced run %s attempt %s for %s", run_id, attempt, repo)
        yield doc

    # Tombstone runs that crossed the lookback boundary or were deleted upstream.
    for old_id in list(run_state.keys()):
        if old_id not in current_ids:
            logger.info("GitHub Actions: run gone %s — tombstoning", old_id)
            yield {"id": old_id, "_deleted": True}
            del run_state[old_id]


def _list_all_runs(client: "Any", repo: str, cutoff: datetime) -> list[dict]:
    """List all workflow runs in the lookback window, subdividing if needed."""
    since = cutoff.strftime("%Y-%m-%dT%H:%M:%SZ")
    until = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return list(_list_runs_window(client, repo, since, until))


def _list_runs_window(
    client: "Any", repo: str, since: str, until: str
) -> Iterator[dict]:
    """Yield runs in a time window; subdivide if the 1,000-result cap is hit."""
    runs: list[dict] = []
    url = f"/repos/{repo}/actions/runs"
    params: dict[str, Any] = {
        "created": f"{since}..{until}",
        "per_page": _PER_PAGE,
    }

    page = 1
    total_fetched = 0
    while True:
        params["page"] = page
        resp = client.get(url, params=params)
        batch = resp.get("workflow_runs", [])
        runs.extend(batch)
        total_fetched += len(batch)

        if len(batch) < _PER_PAGE:
            break  # last page
        page += 1

    if total_fetched >= _RUN_LISTING_LIMIT:
        # We hit the cap — subdivide the window recursively.
        since_dt = _parse_dt(since)
        until_dt = _parse_dt(until)
        mid_dt = since_dt + (until_dt - since_dt) / 2
        if mid_dt <= since_dt:
            # Window is already as narrow as possible — yield what we have.
            yield from runs
            return
        mid = mid_dt.strftime("%Y-%m-%dT%H:%M:%SZ")
        logger.debug(
            "GitHub Actions: run listing limit hit for %s [%s..%s], subdividing.",
            repo,
            since,
            until,
        )
        yield from _list_runs_window(client, repo, since, mid)
        yield from _list_runs_window(client, repo, mid, until)
        return

    yield from runs


def _render_run_doc(repo: str, run: dict) -> dict:
    """Flatten a workflow run API response into a document row."""
    run_id = run["id"]
    attempt = run.get("run_attempt", 1)
    doc_id = _run_doc_id(repo, run_id, attempt)

    lines = [
        f"# GitHub Actions Run: {run.get('name') or run.get('display_title') or run_id}",
        f"Repository: {repo}",
        f"Workflow: {run.get('path') or run.get('workflow_id', '')}",
        f"Branch: {run.get('head_branch', '')}",
        f"Commit: {run.get('head_sha', '')[:12]}",
        f"Event: {run.get('event', '')}",
        f"Actor: {run.get('actor', {}).get('login', '') if isinstance(run.get('actor'), dict) else ''}",
        f"Attempt: {attempt}",
        f"Status: {run.get('status', '')}",
        f"Conclusion: {run.get('conclusion') or '(pending)'}",
        f"Started: {run.get('run_started_at') or run.get('created_at', '')}",
        f"Updated: {run.get('updated_at', '')}",
    ]
    # Duration
    started = run.get("run_started_at") or run.get("created_at")
    updated = run.get("updated_at")
    if started and updated:
        try:
            dur = _parse_dt(updated) - _parse_dt(started)
            lines.append(f"Duration: {int(dur.total_seconds())}s")
        except Exception:
            pass

    run_url = run.get("html_url", "")
    if run_url:
        lines.append(f"URL: {run_url}")

    return {
        "id": doc_id,
        "repo": repo,
        "run_id": str(run_id),
        "attempt": attempt,
        "workflow_path": run.get("path") or str(run.get("workflow_id", "")),
        "branch": run.get("head_branch", ""),
        "status": run.get("status", ""),
        "conclusion": run.get("conclusion") or "",
        "content": "\n".join(lines),
        "_deleted": False,
    }


def _run_doc_id(repo: str, run_id: int | str, attempt: int) -> str:
    return f"run:{repo}:{run_id}:{attempt}"


# ──────────────────────────────────────────────────────────────────────────────
# Job sync
# ──────────────────────────────────────────────────────────────────────────────


def _sync_jobs(
    client: "Any",
    repo: str,
    cutoff: datetime,
    state: dict,
    *,
    include_annotations: bool,
    include_log_tail: bool,
    max_log_lines: int,
) -> Iterator[dict]:
    """Yield job rows for every newly completed run attempt."""
    job_state: dict = state.setdefault("jobs", {}).setdefault(repo, {})
    run_state: dict = state.get("runs", {}).get(repo, {})

    # Only process runs that the run-resource has already marked completed.
    completed_run_ids: set[str] = {
        k for k, v in run_state.items() if v.get("status") == "completed"
    }

    for run_doc_id in completed_run_ids:
        if not run_doc_id.startswith(f"run:{repo}:"):
            continue
        parts = run_doc_id.split(":")
        if len(parts) < 4:
            continue
        run_id_str, attempt_str = parts[2], parts[3]
        run_id = int(run_id_str)
        attempt = int(attempt_str)

        # Skip if jobs for this attempt already stored.
        if job_state.get(run_doc_id):
            continue

        try:
            api_jobs = client.get(
                f"/repos/{repo}/actions/runs/{run_id}/attempts/{attempt}/jobs",
                params={"per_page": _PER_PAGE},
            ).get("jobs", [])
        except Exception as exc:
            logger.warning(
                "GitHub Actions: failed to fetch jobs for %s run %s attempt %s: %s",
                repo,
                run_id,
                attempt,
                exc,
            )
            continue

        job_ids: list[str] = []
        for job in api_jobs:
            doc = _render_job_doc(
                client,
                repo,
                run_id,
                attempt,
                job,
                include_annotations=include_annotations,
                include_log_tail=include_log_tail,
                max_log_lines=max_log_lines,
            )
            job_ids.append(doc["id"])
            yield doc

        job_state[run_doc_id] = job_ids

    # Tombstone jobs whose parent run was tombstoned.
    for run_doc_id, job_ids in list(job_state.items()):
        if run_doc_id not in completed_run_ids and run_doc_id not in run_state:
            for job_doc_id in job_ids:
                yield {"id": job_doc_id, "_deleted": True}
            del job_state[run_doc_id]


def _render_job_doc(
    client: "Any",
    repo: str,
    run_id: int,
    attempt: int,
    job: dict,
    *,
    include_annotations: bool,
    include_log_tail: bool,
    max_log_lines: int,
) -> dict:
    job_id = job["id"]
    doc_id = f"job:{repo}:{run_id}:{attempt}:{job_id}"
    job_name = job.get("name", str(job_id))
    conclusion = job.get("conclusion") or ""
    status = job.get("status", "")
    runner_name = job.get("runner_name") or ""
    runner_labels = job.get("labels") or []

    lines = [
        f"# Job: {job_name}",
        f"Repository: {repo}",
        f"Run: {run_id}, Attempt: {attempt}",
        f"Status: {status}",
        f"Conclusion: {conclusion}",
        f"Runner: {runner_name}",
    ]
    if runner_labels:
        lines.append(f"Runner labels: {', '.join(runner_labels)}")

    # Steps summary
    steps = job.get("steps") or []
    if steps:
        lines.append("\n## Steps")
        for step in steps:
            step_conclusion = step.get("conclusion") or step.get("status", "")
            icon = "✓" if step_conclusion == "success" else ("✗" if step_conclusion == "failure" else "·")
            lines.append(f"  {icon} {step.get('name', '(unnamed)')} [{step_conclusion}]")

    # Annotations for failed jobs
    if include_annotations and conclusion == "failure":
        annotations = _fetch_annotations(client, repo, job)
        if annotations:
            lines.append("\n## Annotations")
            for ann in annotations:
                ann_level = ann.get("annotation_level", "")
                ann_msg = ann.get("message", "")
                ann_path = ann.get("path", "")
                ann_line = ann.get("start_line", "")
                lines.append(f"  [{ann_level}] {ann_path}:{ann_line} — {ann_msg}")

    # Log tail for failing steps
    if include_log_tail and conclusion == "failure":
        log_tail = _fetch_job_log_tail(client, repo, run_id, attempt, job, max_log_lines)
        if log_tail:
            lines.append(f"\n## Log tail (last {max_log_lines} lines)")
            lines.append(log_tail)

    log_available = True  # assumed; flipped to False on 410 below
    job_url = job.get("html_url", "")
    if job_url:
        lines.append(f"\nURL: {job_url}")

    return {
        "id": doc_id,
        "repo": repo,
        "run_id": str(run_id),
        "attempt": attempt,
        "job_id": str(job_id),
        "name": job_name,
        "conclusion": conclusion,
        "log_available": log_available,
        "content": "\n".join(lines),
        "_deleted": False,
    }


def _fetch_annotations(client: "Any", repo: str, job: dict) -> list[dict]:
    """Fetch check-run annotations for a failed job (requires Checks: Read)."""
    check_run_url = job.get("check_run_url", "")
    if not check_run_url:
        return []
    # Extract the check_run_id from the URL tail.
    m = re.search(r"/check-runs/(\d+)$", check_run_url)
    if not m:
        return []
    check_run_id = m.group(1)
    try:
        resp = client.get(
            f"/repos/{repo}/check-runs/{check_run_id}/annotations",
            params={"per_page": _PER_PAGE},
        )
        return resp if isinstance(resp, list) else []
    except _ForbiddenError:
        logger.warning(
            "GitHub Actions: Checks: Read permission missing for %s — skipping annotations.",
            repo,
        )
        return []
    except Exception as exc:
        logger.warning("GitHub Actions: failed to fetch annotations for job %s: %s", job.get("id"), exc)
        return []


def _fetch_job_log_tail(
    client: "Any",
    repo: str,
    run_id: int,
    attempt: int,
    job: dict,
    max_log_lines: int,
) -> str:
    """Fetch log text for the first failing step of a job."""
    steps = job.get("steps") or []
    failing_step = next(
        (s for s in steps if s.get("conclusion") == "failure"),
        None,
    )
    if failing_step is None:
        return ""

    step_number = failing_step.get("number")
    job_id = job.get("id")
    if not step_number or not job_id:
        return ""

    try:
        log_text = client.get_job_step_log(repo, run_id, attempt, job_id, step_number)
    except _GoneError:
        logger.debug("GitHub Actions: logs expired for run %s job %s.", run_id, job_id)
        return ""
    except _ForbiddenError:
        logger.warning("GitHub Actions: no log access for run %s job %s.", run_id, job_id)
        return ""
    except Exception as exc:
        logger.warning("GitHub Actions: failed to fetch log for job %s: %s", job_id, exc)
        return ""

    clean = _ANSI_RE.sub("", log_text)
    lines = clean.splitlines()
    if len(lines) > max_log_lines:
        lines = lines[-max_log_lines:]
    return "\n".join(lines)


# ──────────────────────────────────────────────────────────────────────────────
# HTTP client
# ──────────────────────────────────────────────────────────────────────────────


class _NotFoundError(Exception):
    pass


class _ForbiddenError(Exception):
    pass


class _GoneError(Exception):
    pass


class _RateLimitError(Exception):
    def __init__(self, retry_after: float):
        self.retry_after = retry_after
        super().__init__(f"Rate limited; retry after {retry_after}s")


class _GitHubClient:
    """Thin httpx-based client for the GitHub REST API.

    Handles:
    * Bearer-token auth
    * Conditional requests (ETag / If-None-Match) — avoids burning rate-limit
      quota for unchanged resources.
    * Retry-After / X-RateLimit-Reset honouring
    * Bounded exponential backoff for transient (5xx / network) failures
    * Step-specific log downloads (redirect to a short-lived URL)
    """

    def __init__(self, token: str):
        try:
            import httpx
        except ImportError as exc:
            raise ImportError(_EXTRA_HINT) from exc

        self._token = token
        self._session = httpx.Client(
            base_url=_GITHUB_API_BASE,
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": _ACCEPT_V3,
                "X-GitHub-Api-Version": _API_VERSION_HEADER,
            },
            follow_redirects=True,
            timeout=30.0,
        )
        # ETag cache: url → {"etag": str, "data": Any}
        self._etag_cache: dict[str, dict] = {}

    def get(self, path: str, params: dict | None = None) -> Any:
        """GET the given API path, using ETag caching where applicable."""
        url = path
        cached = self._etag_cache.get(url)
        extra_headers: dict[str, str] = {}
        if cached:
            extra_headers["If-None-Match"] = cached["etag"]

        for attempt in range(_MAX_RETRIES):
            try:
                resp = self._session.get(url, params=params, headers=extra_headers)
            except Exception as exc:
                if attempt == _MAX_RETRIES - 1:
                    raise
                delay = 2.0 ** attempt
                logger.warning("GitHub API network error, retrying in %.1fs: %s", delay, exc)
                time.sleep(delay)
                continue

            if resp.status_code == 304:
                # Not Modified — return the cached payload.
                return cached["data"]  # type: ignore[index]

            if resp.status_code == 200:
                data = resp.json()
                etag = resp.headers.get("ETag")
                if etag:
                    self._etag_cache[url] = {"etag": etag, "data": data}
                return data

            if resp.status_code == 404:
                raise _NotFoundError(path)
            if resp.status_code == 403:
                raise _ForbiddenError(path)
            if resp.status_code == 410:
                raise _GoneError(path)

            if resp.status_code == 429 or resp.status_code in (500, 502, 503, 504):
                retry_after = _parse_retry_after(resp)
                if attempt == _MAX_RETRIES - 1:
                    resp.raise_for_status()
                delay = max(retry_after, 2.0 ** attempt)
                logger.warning(
                    "GitHub API %s — retrying in %.1fs (%d/%d).",
                    resp.status_code,
                    delay,
                    attempt + 1,
                    _MAX_RETRIES,
                )
                time.sleep(delay)
                continue

            resp.raise_for_status()

        raise RuntimeError(f"GitHub API GET {path} failed after {_MAX_RETRIES} attempts.")

    def get_file_content(self, repo: str, path: str) -> str:
        """Download the decoded text of a file from the default branch."""
        import base64

        resp = self.get(f"/repos/{repo}/contents/{path}")
        if isinstance(resp, dict) and resp.get("encoding") == "base64":
            return base64.b64decode(resp["content"].replace("\n", "")).decode("utf-8", errors="replace")
        # Fallback: large files are served via download_url
        download_url = resp.get("download_url") if isinstance(resp, dict) else None
        if download_url:
            raw = self._session.get(download_url)
            raw.raise_for_status()
            return raw.text
        return ""

    def get_job_step_log(
        self, repo: str, run_id: int, attempt: int, job_id: int, step_number: int
    ) -> str:
        """Download logs for a specific step of a specific job attempt.

        GitHub now provides a step-log endpoint; the response is plain text.
        The URL returns a redirect to a short-lived pre-signed URL — httpx
        follows it automatically (follow_redirects=True).
        """
        url = f"/repos/{repo}/actions/jobs/{job_id}/logs"
        # The GitHub API streams the full job log; we can't filter by step via
        # the API, so we download and slice the section for the failing step.
        for attempt_n in range(_MAX_RETRIES):
            try:
                resp = self._session.get(url, headers={"Accept": "application/octet-stream"})
            except Exception as exc:
                if attempt_n == _MAX_RETRIES - 1:
                    raise
                time.sleep(2.0 ** attempt_n)
                continue

            if resp.status_code == 410:
                raise _GoneError(url)
            if resp.status_code == 403:
                raise _ForbiddenError(url)
            if resp.status_code in (200, 302):
                return resp.text
            if resp.status_code in (429, 500, 502, 503, 504):
                if attempt_n == _MAX_RETRIES - 1:
                    resp.raise_for_status()
                time.sleep(max(_parse_retry_after(resp), 2.0 ** attempt_n))
                continue
            resp.raise_for_status()

        return ""


# ──────────────────────────────────────────────────────────────────────────────
# Utilities
# ──────────────────────────────────────────────────────────────────────────────


def _parse_retry_after(resp) -> float:
    """Return seconds to wait from Retry-After or X-RateLimit-Reset."""
    ra = resp.headers.get("Retry-After")
    if ra:
        try:
            return float(ra)
        except ValueError:
            pass
    reset = resp.headers.get("X-RateLimit-Reset")
    if reset:
        try:
            delta = float(reset) - time.time()
            return max(delta, 1.0)
        except ValueError:
            pass
    return 1.0


def _parse_dt(s: str) -> datetime:
    """Parse an ISO-8601 datetime string (with or without timezone)."""
    s = s.rstrip("Z")
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        # Try without microseconds
        dt = datetime.strptime(s, "%Y-%m-%dT%H:%M:%S")
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _stable_hash(*parts: str) -> str:
    """Short deterministic hash for composite IDs."""
    return hashlib.sha1(":".join(parts).encode()).hexdigest()[:12]
