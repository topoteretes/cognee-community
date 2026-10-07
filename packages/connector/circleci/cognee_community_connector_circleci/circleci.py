"""CircleCI connector for cognee: pipeline, workflow and job outcomes as memory.

One document per pipeline. Failed jobs carry their failing tests, never build
logs. Incremental by pipeline ``created_at``; forget-on-delete via ``_deleted``.
"""

from __future__ import annotations

import functools
import time
from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta
from typing import Any

import requests
from cognee.shared.logging_utils import get_logger

logger = get_logger("circleci_connector")

# CircleCI API v2. Overridable for CircleCI server installs.
DEFAULT_BASE_URL = "https://circleci.com/api/v2"

# Document-source tag: routes rows through cognify instead of the dlt-row path.
CIRCLECI_SOURCE_NAME = "circleci"

# Attempts per request (first try + retries) for rate-limited / transient failures.
_MAX_RETRIES = 5
_RETRY_STATUSES = frozenset({429, 500, 502, 503, 504})
# requests waits forever without a timeout; a hung connection would stall the sync.
_TIMEOUT_SECONDS = 30

# Failure output caps, so a badly broken build can't dominate the token budget.
DEFAULT_MAX_FAILING_TESTS = 20
DEFAULT_MAX_MESSAGE_CHARS = 500

# Workflow statuses, most telling first: a pipeline with any unfinished workflow
# reads as unfinished, otherwise the worst final outcome wins.
_STATUS_PRIORITY = (
    "failing",
    "running",
    "on_hold",
    "error",
    "failed",
    "unauthorized",
    "canceled",
    "not_run",
    "success",
)

# Workflow statuses that will not change any more. Anything else (running,
# failing, on_hold, or a status added later) means "check again next sync".
_FINAL_WORKFLOW_STATUSES = frozenset(
    {"success", "failed", "error", "canceled", "not_run", "unauthorized"}
)
# Pipeline states before CircleCI has created the workflows.
_SETUP_PIPELINE_STATES = frozenset({"setup-pending", "setup", "pending"})

# First sync of a project: how many of its most recent pipelines to ingest.
# Later syncs take everything since the cursor.
DEFAULT_MAX_INITIAL_PIPELINES = 50
# Stop re-checking a pipeline that is still unfinished after this long, e.g. an
# approval nobody clicks. Its last emitted row stays in memory.
DEFAULT_PENDING_TIMEOUT_DAYS = 7

# Slug prefix → path segment in the CircleCI web app's URLs.
_APP_VCS_SEGMENT = {"gh": "github", "bb": "bitbucket"}


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------
def _make_session(token: str) -> requests.Session:
    """Build a ``requests`` session authenticated with a CircleCI personal API token."""
    session = requests.Session()
    session.headers.update({"Circle-Token": token, "Accept": "application/json"})
    return session


def _api_get(session: Any, base_url: str, path: str, params: dict | None = None) -> dict:
    """GET an API v2 path and return its JSON, retrying rate limits and transient errors.

    429 and 5xx responses wait for ``Retry-After`` (else exponential backoff), as
    do connection errors and timeouts. Any other error status, such as the 404
    for a project that no longer exists, raises ``requests.HTTPError`` at once so
    the caller decides what it means.
    """
    url = f"{base_url}{path}"
    attempt = 0
    while True:
        last_attempt = attempt == _MAX_RETRIES - 1
        try:
            response = session.get(url, params=params or {}, timeout=_TIMEOUT_SECONDS)
        except (requests.ConnectionError, requests.Timeout) as exc:
            if last_attempt:
                raise
            reason, delay = repr(exc), _retry_after(None, attempt)
        else:
            if response.status_code not in _RETRY_STATUSES or last_attempt:
                response.raise_for_status()
                return response.json()
            reason, delay = f"HTTP {response.status_code}", _retry_after(response.headers, attempt)

        logger.warning(
            "CircleCI: %s on %s — retrying in %.1fs (%d/%d).",
            reason,
            path,
            delay,
            attempt + 1,
            _MAX_RETRIES - 1,
        )
        time.sleep(delay)
        attempt += 1


def _retry_after(headers: Any, attempt: int) -> float:
    """Seconds to wait before retrying: the Retry-After header, else exponential backoff."""
    try:
        return float((headers or {}).get("Retry-After"))
    except (TypeError, ValueError):
        return float(2**attempt)


def _paginate(session: Any, base_url: str, path: str, params: dict | None = None) -> Iterator[dict]:
    """Yield ``items`` across pages, following ``next_page_token``.

    Lazy on purpose: the pipeline listing is newest-first, so the sync can stop
    once it passes the cursor without fetching older pages.
    """
    params = dict(params or {})
    while True:
        data = _api_get(session, base_url, path, params)
        yield from data.get("items") or []
        page_token = data.get("next_page_token")
        if not page_token:
            return
        params["page-token"] = page_token


# ---------------------------------------------------------------------------
# Pipeline → document row
# ---------------------------------------------------------------------------
def _fetch_workflows(
    session: Any, base_url: str, project_slug: str, pipeline_id: str
) -> list[dict]:
    """A pipeline's workflows, each with its ``jobs``; failed jobs also get ``failing_tests``.

    Test results are fetched for failed jobs only. Passing jobs become a status
    line, and build logs are never read.
    """
    workflows = list(_paginate(session, base_url, f"/pipeline/{pipeline_id}/workflow"))
    for workflow in workflows:
        workflow["jobs"] = list(_paginate(session, base_url, f"/workflow/{workflow['id']}/job"))
        for job in workflow["jobs"]:
            if job.get("status") == "failed":
                tests = _paginate(
                    session, base_url, f"/project/{project_slug}/{job['job_number']}/tests"
                )
                # The endpoint returns every test (passed and skipped too).
                job["failing_tests"] = [t for t in tests if t.get("result") == "failure"]
    return workflows


def _pipeline_to_row(
    pipeline: dict,
    workflows: list[dict],
    *,
    max_failing_tests: int = DEFAULT_MAX_FAILING_TESTS,
    max_message_chars: int = DEFAULT_MAX_MESSAGE_CHARS,
) -> dict[str, Any]:
    """Flatten a pipeline and its fetched workflows into a document row.

    Document-mode rows are ``{id, title, content, url}``: cognee only turns
    ``title`` and ``content`` into text, so everything worth remembering goes
    into ``content``. Durations and other timestamps are left out, so a
    finished pipeline's text never changes and is never re-cognified.
    """
    vcs = pipeline.get("vcs") or {}
    ref = vcs.get("branch") or vcs.get("tag")
    title = f"{pipeline.get('project_slug')} pipeline #{pipeline.get('number')}"
    if ref:
        title += f" on {ref}"
    title += f": {_overall_status(pipeline, workflows)}"

    return {
        "id": str(pipeline["id"]),
        "title": title,
        "content": _render_content(pipeline, workflows, max_failing_tests, max_message_chars),
        "url": _pipeline_url(pipeline),
        # Hard-delete marker (always False for live pipelines).
        "_deleted": False,
    }


def _overall_status(pipeline: dict, workflows: list[dict]) -> str:
    """One status for the whole pipeline, from its workflows.

    The pipeline's own ``state`` only covers config processing (it stays
    ``created`` after every workflow finishes), so it is used only when there
    are no workflows, e.g. ``errored`` for an invalid config.
    """
    statuses = {str(w.get("status")) for w in workflows}
    if not statuses:
        return pipeline.get("state") or "unknown"
    return next((s for s in _STATUS_PRIORITY if s in statuses), min(statuses))


def _is_finished(pipeline: dict, workflows: list[dict]) -> bool:
    """True once nothing about the pipeline will change.

    Decided from the workflows, not the pipeline ``state`` (which stays
    ``created`` while they run). A pipeline with no workflows is finished unless
    CircleCI is still setting it up: ``errored`` (bad config) and ``created``
    with every workflow filtered out are both final.
    """
    if pipeline.get("state") in _SETUP_PIPELINE_STATES:
        return False
    return all(w.get("status") in _FINAL_WORKFLOW_STATUSES for w in workflows)


def _render_content(
    pipeline: dict, workflows: list[dict], max_failing_tests: int, max_message_chars: int
) -> str:
    vcs = pipeline.get("vcs") or {}
    trigger = pipeline.get("trigger") or {}
    actor = (trigger.get("actor") or {}).get("login")

    lines = [
        f"Project: {pipeline.get('project_slug')}",
        f"Pipeline #{pipeline.get('number')}, created {_format_time(pipeline.get('created_at'))}",
        f"Trigger: {trigger.get('type') or 'unknown'}" + (f" by {actor}" if actor else ""),
    ]
    if vcs.get("branch"):
        lines.append(f"Branch: {vcs['branch']}")
    if vcs.get("tag"):
        lines.append(f"Tag: {vcs['tag']}")
    if vcs.get("revision"):
        subject = (vcs.get("commit") or {}).get("subject")
        lines.append(f"Commit: {vcs['revision'][:7]}" + (f" {subject}" if subject else ""))
    lines.extend(
        f"Error ({error.get('type')}): {error.get('message')}"
        for error in pipeline.get("errors") or []
    )

    for workflow in workflows:
        lines += ["", f"Workflow {workflow.get('name')}: {workflow.get('status')}"]
        for job in workflow.get("jobs") or []:
            lines += _render_job(job, max_failing_tests, max_message_chars)
    return "\n".join(lines)


def _render_job(job: dict, max_failing_tests: int, max_message_chars: int) -> list[str]:
    status_line = f"- {job.get('name')}: {job.get('status')}"
    if "failing_tests" not in job:
        return [status_line]

    failing = job["failing_tests"]
    if not failing:
        # Failed without stored test results (no store_test_results step).
        return [f"{status_line} (no test results)"]

    shown = failing[:max_failing_tests]
    count = (
        str(len(failing)) if len(shown) == len(failing) else f"{len(failing)}, showing {len(shown)}"
    )
    lines = [status_line, f"  Failing tests ({count}):"]
    for test in shown:
        lines.append(f"  - {_test_name(test)}")
        message = _message_tail(test.get("message") or "", max_message_chars)
        lines.extend(f"      {text}" for text in message.splitlines() if text.strip())
    return lines


def _test_name(test: dict) -> str:
    name = test.get("name") or "unnamed test"
    location = test.get("file") or test.get("classname")
    return f"{location}::{name}" if location else name


def _message_tail(message: str, max_chars: int) -> str:
    """The last ``max_chars`` of a failure message, starting on a whole line when possible.

    Test runners print the error and its location last (pytest ends with
    ``E   KeyError: 'timeout'`` then ``tests/x.py:26: KeyError``), while the start
    is often just the test's source, so the end is the part worth keeping.
    """
    message = message.strip()
    if len(message) <= max_chars:
        return message
    tail = message[-max_chars:]
    _, newline, rest = tail.partition("\n")
    return f"...\n{rest}" if newline and rest.strip() else f"...{tail}"


def _format_time(timestamp: str | None) -> str:
    """``2026-10-07T21:32:18.514Z`` → ``2026-10-07 21:32 UTC``."""
    if not timestamp:
        return "unknown"
    return f"{timestamp[:16].replace('T', ' ')} UTC"


def _pipeline_url(pipeline: dict) -> str:
    """The pipeline's page in the CircleCI web app (circleci.com, not server installs)."""
    vcs, _, rest = (pipeline.get("project_slug") or "").partition("/")
    return (
        f"https://app.circleci.com/pipelines/{_APP_VCS_SEGMENT.get(vcs, vcs)}/{rest}/"
        f"{pipeline.get('number')}"
    )


# ---------------------------------------------------------------------------
# Sync (pure given a session + state dict — unit-testable)
# ---------------------------------------------------------------------------
def sync_pipelines(
    session: Any,
    base_url: str,
    state: dict,
    *,
    project_slugs: list[str],
    branch: str | None = None,
    max_initial_pipelines: int | None = DEFAULT_MAX_INITIAL_PIPELINES,
    pending_timeout_days: float = DEFAULT_PENDING_TIMEOUT_DAYS,
    max_failing_tests: int = DEFAULT_MAX_FAILING_TESTS,
    max_message_chars: int = DEFAULT_MAX_MESSAGE_CHARS,
    now: datetime | None = None,
) -> Iterator[dict[str, Any]]:
    """Yield a row for every new pipeline, and again for each one that has since finished.

    ``state`` keeps one entry per project slug under ``state["projects"]``:

    * ``cursor``: the newest pipeline ``created_at`` seen so far.
    * ``pending``: ids of pipelines that were unfinished at the last sync.

    Each run first re-checks the pending pipelines and emits a row only for
    those that have finished, so their final status lands without re-ingesting
    every in-between state. It then pages the newest-first pipeline listing and
    stops at the cursor.
    """
    to_row = functools.partial(
        _pipeline_to_row,
        max_failing_tests=max_failing_tests,
        max_message_chars=max_message_chars,
    )
    projects = state.setdefault("projects", {})
    for slug in project_slugs:
        yield from _sync_project(
            session,
            base_url,
            slug,
            projects.setdefault(slug, {}),
            to_row=to_row,
            branch=branch,
            max_initial_pipelines=max_initial_pipelines,
            pending_timeout=timedelta(days=pending_timeout_days),
            now=now or datetime.now(UTC),
        )


def _sync_project(
    session: Any,
    base_url: str,
    slug: str,
    project_state: dict,
    *,
    to_row: Callable[[dict, list[dict]], dict[str, Any]],
    branch: str | None,
    max_initial_pipelines: int | None,
    pending_timeout: timedelta,
    now: datetime,
) -> Iterator[dict[str, Any]]:
    cursor: str | None = project_state.get("cursor")
    still_pending: list[str] = []
    new = finished = 0

    # 1. Pipelines unfinished at the last sync: emit again only once finished.
    for pipeline_id in project_state.get("pending", []):
        try:
            pipeline = _api_get(session, base_url, f"/pipeline/{pipeline_id}")
        except requests.HTTPError as exc:
            if exc.response is None or exc.response.status_code != 404:
                raise
            logger.info("CircleCI %s: pending pipeline %s is gone, dropping it.", slug, pipeline_id)
            continue

        workflows = _fetch_workflows(session, base_url, slug, pipeline_id)
        if _is_finished(pipeline, workflows):
            yield to_row(pipeline, workflows)
            finished += 1
        elif now - _parse_time(pipeline["created_at"]) > pending_timeout:
            logger.warning(
                "CircleCI %s: pipeline #%s still unfinished after %s, no longer re-checking it.",
                slug,
                pipeline.get("number"),
                pending_timeout,
            )
        else:
            still_pending.append(pipeline_id)

    # 2. Pipelines created since the cursor. The listing is newest-first, so stop
    #    at the first one at or before the cursor (later pages are never fetched).
    newest = cursor
    params = {"branch": branch} if branch else {}
    listing = _paginate(session, base_url, f"/project/{slug}/pipeline", params)
    for seen, pipeline in enumerate(listing):
        created = _parse_time(pipeline["created_at"])
        if cursor is not None and created <= _parse_time(cursor):
            break
        if cursor is None and max_initial_pipelines is not None and seen >= max_initial_pipelines:
            break

        workflows = _fetch_workflows(session, base_url, slug, pipeline["id"])
        yield to_row(pipeline, workflows)
        new += 1
        if not _is_finished(pipeline, workflows):
            still_pending.append(pipeline["id"])
        if newest is None or created > _parse_time(newest):
            newest = pipeline["created_at"]

    project_state["cursor"] = newest
    project_state["pending"] = still_pending
    logger.info(
        "CircleCI %s: %d new pipeline(s), %d finished since last sync, %d still pending.",
        slug,
        new,
        finished,
        len(still_pending),
    )


def _parse_time(timestamp: str) -> datetime:
    # Parsed rather than compared as strings: as text, "21:33:18Z" sorts after
    # "21:33:18.514Z" even though it is earlier.
    return datetime.fromisoformat(timestamp)


def circleci_source(
    *,
    project_slugs: list[str],
    token: str | None = None,
    branch: str | None = None,
    base_url: str = DEFAULT_BASE_URL,
    session: Any = None,
):
    """Return a ``dlt`` resource that yields CircleCI pipelines for ``cognee.remember``.

    Args:
        project_slugs: Projects to sync, e.g. ``["gh/org/repo"]`` or
            ``["circleci/<org-id>/<project-id>"]`` for GitHub App projects.
        token: CircleCI personal API token. Falls back to ``CIRCLECI_TOKEN``.
        branch: Only sync pipelines on this branch.
        base_url: API base URL.
        session: Pre-built ``requests`` session (test injection point).
    """
    raise NotImplementedError
