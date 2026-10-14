"""DLT source for GitHub Actions workflow runs (full-snapshot sync + forget-on-delete).

Fetches completed workflow runs (and their jobs) for the configured
repositories, renders each run to markdown, and yields them as a dlt resource
for cognee's ingestion pipeline.

Runs are ingested as *normal documents*: the source declares
``cognee_document_source = "github_actions"``, so ``resolve_dlt_sources`` tags
each row ``external_metadata["source"] = "github_actions"`` (not ``"dlt"``) and
routes it through the standard cognify entity-extraction pipeline instead of the
deterministic dlt-row schema-context path.

The source is a full snapshot: ``write_disposition="replace"`` rewrites staging
with exactly the runs inside the configured window each sync. Runs that fall out
of the window (``max_runs_per_repo`` / ``created_after``), age out of GitHub's
retention period, or are deleted simply drop out of the snapshot, and cognee's
existing ``orphan_cleanup`` removes them from the graph and vector stores. Only
*completed* runs are ingested, so a run's content is stable once synced and
unchanged runs keep a stable content-hash ``data_id`` (no re-cognify). A re-run
changes the run's attempt and conclusion, so it is picked up as an edit.
"""

import os
import re
import time
from concurrent.futures import ThreadPoolExecutor
from itertools import islice
from typing import Any

from cognee.shared.logging_utils import get_logger
from cognee.tasks.ingestion.dlt_utils import DOCUMENT_SOURCE_ATTR

logger = get_logger("github_actions_connector")

# dlt resource / staging-table name for workflow runs.
GITHUB_ACTIONS_TABLE_NAME = "github_actions_runs"
GITHUB_ACTIONS_SOURCE_NAME = "github_actions"
_GITHUB_API_URL = "https://api.github.com"
# Pin the REST API version so upstream changes can't silently alter parsing.
_GITHUB_API_VERSION = "2022-11-28"

# GitHub's maximum page size for the runs / jobs listings.
_PER_PAGE = 100
# Retry budget for rate-limited / transient GitHub API responses.
_MAX_RETRIES = 5
# Longest we are willing to sleep for a rate-limit reset before giving up. A
# primary rate limit can take up to an hour to reset; blocking a sync that long
# is worse than failing it (staging is left untouched on failure).
_MAX_RETRY_WAIT = 60.0
_TRANSIENT_STATUSES = (429, 500, 502, 503, 504)

# Each log line is prefixed with an ISO timestamp; strip it to cut noise.
_LOG_TIMESTAMP = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?Z ")
# Terminal colour/formatting escape sequences that runners write into logs.
_ANSI_ESCAPE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
# Workflow-command markers GitHub writes into job logs.
_LOG_ERROR = "##[error]"
_LOG_GROUP = "##[group]"
_LOG_ENDGROUP = "##[endgroup]"
# Cap on distinct error lines kept per failed job.
_MAX_LOG_ERRORS = 20


def github_actions_source(
    repos: list[str],
    token: str | None = None,
    workflow: str | int | None = None,
    branch: str | None = None,
    created_after: str | None = None,
    max_runs_per_repo: int | None = 100,
    include_failed_logs: bool = False,
    log_tail_lines: int = 50,
    max_workers: int = 4,
    client: Any = None,
):
    """Create a dlt source that yields GitHub Actions workflow runs as markdown documents.

    Args:
        repos: Repositories to ingest, as ``"owner/repo"``.
        token: GitHub token with ``actions:read`` (plus ``repo`` for private
            repositories). Falls back to ``GITHUB_TOKEN``.
        workflow: Restrict ingestion to one workflow (file name such as
            ``"ci.yml"``, or its numeric id).
        branch: Restrict ingestion to runs on this branch.
        created_after: Only ingest runs created on/after this date
            (``YYYY-MM-DD``).
        max_runs_per_repo: Snapshot window — the most recent N completed runs
            per repository. ``None`` ingests every run in range (one jobs
            request per run, so this can be slow on busy repositories).
        include_failed_logs: Also fetch the log of each failed job and keep its
            error lines plus the failed step's last output lines. Off by
            default: it costs one extra request per failed job, and logs can
            contain anything your workflows print.
        log_tail_lines: How many output lines to keep from each failed step.
        max_workers: How many runs to fetch jobs/logs for concurrently. GitHub
            asks clients to keep concurrency low (secondary rate limits are
            retried); ``1`` fetches serially.
        client: Pre-built ``httpx.Client`` pointed at the GitHub API (mainly a
            test-injection point); when omitted one is built from the token.

    Returns:
        A dlt source suitable for ``cognee.add(...)`` / ``cognee.remember(...)``.
    """
    import dlt
    import httpx

    if max_workers < 1:
        raise ValueError("max_workers must be at least 1.")
    if not repos:
        raise ValueError("At least one repository is required: repos=['owner/repo'].")
    for repo in repos:
        if repo.count("/") != 1 or not all(repo.split("/")):
            raise ValueError(f"Invalid repository {repo!r}: expected 'owner/repo'.")

    if client is None:
        resolved_token = token or os.environ.get("GITHUB_TOKEN")
        if not resolved_token:
            raise ValueError("GitHub token required: pass token= or set GITHUB_TOKEN.")
        client = httpx.Client(
            base_url=_GITHUB_API_URL,
            headers={
                "Authorization": f"Bearer {resolved_token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": _GITHUB_API_VERSION,
            },
            # Job logs are served via a redirect to a signed download URL;
            # httpx drops the Authorization header on the cross-origin hop.
            follow_redirects=True,
            timeout=30.0,
        )

    @dlt.resource(name=GITHUB_ACTIONS_TABLE_NAME, primary_key="id", write_disposition="replace")
    def github_actions_runs():
        # Full-snapshot sync: errors are NOT swallowed. Staging is
        # authoritative under replace, so a run missing from a partial snapshot
        # would be forgotten as if deleted. Letting the error abort the run
        # leaves staging — and memory — untouched, which is the safe failure.
        # Transient blips are already retried in _request.
        #
        # Each run costs one jobs request (plus one per failed job's log), so
        # runs are rendered concurrently, one page at a time. executor.map
        # keeps the listing order and re-raises the first failure.
        count = 0
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            for repo in repos:
                runs = _iter_runs(client, repo, workflow, branch, created_after, max_runs_per_repo)
                while batch := list(islice(runs, _PER_PAGE)):
                    rows = executor.map(
                        lambda run, repo=repo: _run_to_row(
                            client, repo, run, include_failed_logs, log_tail_lines
                        ),
                        batch,
                    )
                    for row in rows:
                        count += 1
                        yield row
        logger.info("GitHub Actions: synced %d run(s).", count)

    @dlt.source(name=GITHUB_ACTIONS_SOURCE_NAME)
    def _github_actions():
        return github_actions_runs

    source = _github_actions()
    # Opt into the document ingestion path (run → text document → cognify).
    # resolve_dlt_sources reads this marker; it never imports this connector.
    setattr(source, DOCUMENT_SOURCE_ATTR, GITHUB_ACTIONS_SOURCE_NAME)
    return source


# ---------------------------------------------------------------------------
# GitHub API helpers (module-private)
# ---------------------------------------------------------------------------


def _request(client, url: str, params: dict | None = None):
    """GET a GitHub API url, retrying rate-limit / transient errors.

    Rate-limit (429, or 403 with an exhausted/secondary limit), server (5xx),
    and network errors are retried with backoff; permanent errors (auth,
    not-found, permission) and exhausted retries raise so the sync aborts.
    """
    import httpx

    for attempt in range(_MAX_RETRIES):
        try:
            response = client.get(url, params=params)
        except httpx.TransportError as exc:
            if attempt == _MAX_RETRIES - 1:
                raise
            delay = float(2**attempt)
            logger.warning(
                "GitHub: %s — retrying in %.1fs (%d/%d).", exc, delay, attempt + 1, _MAX_RETRIES
            )
            time.sleep(delay)
            continue

        if not _is_rate_limited(response) and response.status_code not in _TRANSIENT_STATUSES:
            response.raise_for_status()
            return response

        delay = _retry_after(response.headers, attempt)
        if attempt == _MAX_RETRIES - 1 or delay > _MAX_RETRY_WAIT:
            response.raise_for_status()
        logger.warning(
            "GitHub: HTTP %d on %s — retrying in %.1fs (%d/%d).",
            response.status_code,
            url,
            delay,
            attempt + 1,
            _MAX_RETRIES,
        )
        time.sleep(delay)

    raise AssertionError("unreachable")  # pragma: no cover


def _is_rate_limited(response) -> bool:
    """True for a 403/429 caused by a primary or secondary rate limit."""
    if response.status_code == 429:
        return True
    if response.status_code != 403:
        return False
    # A plain 403 is a permission error (not retryable); a rate-limited 403
    # carries an exhausted quota or a Retry-After header.
    return response.headers.get("x-ratelimit-remaining") == "0" or "retry-after" in response.headers


def _retry_after(headers, attempt: int) -> float:
    """Seconds to wait before retrying: Retry-After, else the rate-limit reset, else backoff."""
    try:
        return float(headers["retry-after"])
    except (KeyError, TypeError, ValueError):
        pass
    if headers.get("x-ratelimit-remaining") == "0":
        try:
            return max(0.0, float(headers["x-ratelimit-reset"]) - time.time())
        except (KeyError, TypeError, ValueError):
            pass
    return float(2**attempt)


def _paginate(client, url: str, key: str, params: dict | None = None):
    """Yield items under ``key`` across GitHub's Link-header pagination."""
    next_url: str | None = url
    next_params = {"per_page": _PER_PAGE, **(params or {})}
    while next_url:
        response = _request(client, next_url, next_params)
        yield from response.json().get(key) or []
        # The next link already carries every query parameter.
        next_url = response.links.get("next", {}).get("url")
        next_params = None


def _iter_runs(client, repo, workflow, branch, created_after, max_runs):
    """Yield completed workflow-run objects for ``repo``, newest first."""
    if workflow is not None:
        url = f"/repos/{repo}/actions/workflows/{workflow}/runs"
    else:
        url = f"/repos/{repo}/actions/runs"

    # Completed runs only: an in-progress run's status/conclusion still changes,
    # which would churn its content hash and re-cognify it on every sync.
    params: dict = {"status": "completed"}
    if branch:
        params["branch"] = branch
    if created_after:
        params["created"] = f">={created_after}"

    for count, run in enumerate(_paginate(client, url, "workflow_runs", params)):
        if max_runs is not None and count >= max_runs:
            return
        yield run


def _iter_jobs(client, repo, run_id):
    """Yield the jobs of the run's latest attempt."""
    yield from _paginate(client, f"/repos/{repo}/actions/runs/{run_id}/jobs", "jobs")


def _job_log(client, repo, job_id) -> str:
    """Return a job's raw log text, or "" if it is unavailable."""
    import httpx

    try:
        response = _request(client, f"/repos/{repo}/actions/jobs/{job_id}/logs")
    except httpx.HTTPStatusError as exc:
        # Logs expire (404/410) independently of the run; a missing log only
        # thins the document, it does not make the run look deleted.
        if exc.response.status_code in (404, 410):
            logger.warning("GitHub: log for job %s in %s is unavailable.", job_id, repo)
            return ""
        raise
    return response.text


def _clean_log_line(line: str) -> str:
    """Strip the BOM, timestamp prefix, and terminal escape codes from a log line."""
    return _ANSI_ESCAPE.sub("", _LOG_TIMESTAMP.sub("", line.lstrip("\ufeff")))


def _summarize_log(text: str, max_lines: int) -> tuple[list[str], list[str]]:
    """Reduce a job log to ``(errors, output)``.

    ``errors`` are the distinct ``##[error]`` messages. ``output`` is the last
    ``max_lines`` lines the failed step printed before its first error — the
    runner setup, the step's command/env header, and log-group markers are
    dropped. A log without error markers falls back to its last lines.
    """
    lines = [_clean_log_line(line) for line in text.splitlines()]

    errors: list[str] = []
    for line in lines:
        message = line[len(_LOG_ERROR) :].strip() if line.startswith(_LOG_ERROR) else ""
        if message and message not in errors and len(errors) < _MAX_LOG_ERRORS:
            errors.append(message)

    first_error = next((i for i, line in enumerate(lines) if line.startswith(_LOG_ERROR)), None)
    if first_error is None:
        step_output = lines
    else:
        # The failed step starts at the last "##[group]Run ..." header before
        # its first error; that header group (command echo, shell, env) ends
        # at the next "##[endgroup]", after which the step's own output begins.
        start = 0
        for i in range(first_error - 1, -1, -1):
            if lines[i].startswith(f"{_LOG_GROUP}Run "):
                start = i
                break
        for i in range(start, first_error):
            if lines[i] == _LOG_ENDGROUP:
                start = i + 1
                break
        step_output = lines[start:first_error]

    output = [line for line in step_output if line.strip() and not line.startswith("##[")]
    return errors, output[-max_lines:] if max_lines > 0 else []


def _run_to_row(client, repo, run, include_failed_logs: bool, log_tail_lines: int) -> dict:
    """Flatten a workflow run (plus its jobs) into a document row.

    Only ``title``/``content`` (+ ``id``/``url`` for identity and provenance)
    are kept; every rendered field is fixed once a run completes, so re-syncing
    an unchanged run does not churn its content-hash data_id.
    """
    jobs = list(_iter_jobs(client, repo, run.get("id")))
    return {
        "id": f"{repo}:{run.get('id')}",
        "url": run.get("html_url"),
        "title": _run_title(repo, run),
        "content": _render_run(client, repo, run, jobs, include_failed_logs, log_tail_lines),
    }


def _run_title(repo: str, run: dict) -> str:
    """``"<workflow> #<n> — <conclusion> (<repo>)"``."""
    name = run.get("name") or run.get("path") or "workflow"
    return f"{name} #{run.get('run_number')} — {run.get('conclusion') or 'unknown'} ({repo})"


def _render_run(client, repo, run, jobs, include_failed_logs: bool, log_tail_lines: int) -> str:
    """Render a run and its jobs to markdown."""
    commit_message = ((run.get("head_commit") or {}).get("message") or "").splitlines()
    sha = (run.get("head_sha") or "")[:7]

    lines = [
        f"Repository: {repo}",
        f"Workflow: {run.get('name')} ({run.get('path')})",
        f"Run: #{run.get('run_number')}, attempt {run.get('run_attempt')}",
        f"Conclusion: {run.get('conclusion')}",
        f"Event: {run.get('event')}",
        f"Branch: {run.get('head_branch')}",
        f"Commit: {sha} {commit_message[0] if commit_message else ''}".rstrip(),
        f"Triggered by: {(run.get('triggering_actor') or run.get('actor') or {}).get('login')}",
        f"Started: {run.get('run_started_at')}",
        f"Finished: {run.get('updated_at')}",
    ]
    if run.get("display_title"):
        lines.append(f"Title: {run['display_title']}")

    if jobs:
        lines += ["", "## Jobs"]
        for job in jobs:
            lines.append(f"- {job.get('name')}: {job.get('conclusion')}")
            for step in job.get("steps") or []:
                if step.get("conclusion") == "failure":
                    lines.append(f"  - failed step: {step.get('name')}")

    if include_failed_logs:
        for job in jobs:
            if job.get("conclusion") != "failure":
                continue
            errors, output = _summarize_log(_job_log(client, repo, job.get("id")), log_tail_lines)
            if not errors and not output:
                continue
            lines += ["", f"## Failure log: {job.get('name')}"]
            if errors:
                lines += ["Errors:"] + [f"- {message}" for message in errors]
            if output:
                lines += ["Output before the error:", "```text", *output, "```"]

    return "\n".join(lines)
