"""CircleCI connector for cognee: pipeline, workflow and job outcomes as memory.

One document per pipeline. Failed jobs carry their failing tests, never build
logs. Incremental by pipeline ``created_at``; forget-on-delete via ``_deleted``.
"""

from __future__ import annotations

import functools
import itertools
import re
import time
from collections.abc import Callable, Iterable, Iterator
from datetime import datetime, timedelta, timezone
from typing import Any

import dlt
import requests
from cognee.shared.logging_utils import get_logger
from cognee.tasks.ingestion import dlt_utils
from cognee.tasks.ingestion.dlt_utils import DOCUMENT_SOURCE_ATTR

logger = get_logger("circleci_connector")

# CircleCI API v2. Overridable for CircleCI server installs.
DEFAULT_BASE_URL = "https://circleci.com/api/v2"

# Document-source tag: routes rows through cognify instead of the dlt-row path.
CIRCLECI_SOURCE_NAME = "circleci"
# Default dlt resource / staging-table name. Every CircleCI source syncing into
# one dataset needs its own (see circleci_source's ``resource_name``).
CIRCLECI_TABLE_NAME = "circleci_pipelines"

# Attempts per request (first try + retries) for rate-limited / transient failures.
_MAX_RETRIES = 5
_RETRY_STATUSES = frozenset({429, 500, 502, 503, 504})
# Longest Retry-After honoured. The sync runs under cognee's process-wide
# staging lock, so a 900s Retry-After would stall every other ingestion.
_MAX_RETRY_AFTER_SECONDS = 60.0
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
# Watch a finished pipeline for reruns for this long after it was created.
# Rerunning a workflow adds a new workflow to the same pipeline, which is
# already past the cursor, so without this the rerun's outcome never lands.
DEFAULT_RERUN_WINDOW_DAYS = 7

# Count-only diagnostics published on the resource as ``cognee_sync_stats``.
_STAT_KEYS = ("scanned", "skipped", "failed", "deleted", "failed_rate_limit")

# Slug prefix → path segment in the CircleCI web app's URLs.
_APP_VCS_SEGMENT = {"gh": "github", "bb": "bitbucket"}

# Failure output is stored as-is, so a test that prints a credential would put
# it in the graph. These mask the common shapes; a safety net, not a scanner.
_REDACTED = "[REDACTED]"
_SECRET_PATTERNS = (
    # key=value / key: value, where the key names a secret.
    (
        re.compile(
            r"(?i)\b([\w.-]*(?:token|secret|passw(?:or)?d|api[_-]?key|access[_-]?key"
            r"|private[_-]?key|credentials?)[\w.-]*[\"']?\s*[:=]\s*[\"']?)([^\s\"',;]{4,})"
        ),
        rf"\1{_REDACTED}",
    ),
    # Authorization header values.
    (re.compile(r"(?i)\b((?:bearer|basic)\s+)([\w\-.~+/]{8,}=*)"), rf"\1{_REDACTED}"),
    # Credentials in URLs: scheme://user:password@host.
    (re.compile(r"(?i)(\b[a-z][a-z0-9+.-]*://[^\s/:@]+:)([^\s/@]+)(@)"), rf"\1{_REDACTED}\3"),
    # Well-known token formats, wherever they appear.
    (
        re.compile(
            r"\b(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_\w{30,}|CCIPAT_\w{20,}|glpat-[\w-]{20,}"
            r"|AKIA[0-9A-Z]{16}|xox[abposr]-[\w-]{10,}|sk-[\w-]{20,}"
            r"|eyJ[\w-]{8,}\.eyJ[\w-]{8,}\.[\w-]{8,})"
        ),
        _REDACTED,
    ),
)


# ---------------------------------------------------------------------------
# Errors: messages carry the HTTP status only. The request path, which names
# the project, is kept on the exception, never in its message.
# ---------------------------------------------------------------------------
class CircleCIError(RuntimeError):
    """Base class of the errors this connector raises."""


class CircleCIAPIError(CircleCIError):
    """CircleCI answered with an error status, or could not be reached."""

    def __init__(self, message: str, *, status: int | None = None, path: str | None = None):
        super().__init__(message)
        self.status = status
        self.path = path


class CircleCIAuthError(CircleCIAPIError):
    """CircleCI rejected the token (HTTP 401 or 403)."""


class CircleCINotFoundError(CircleCIAPIError):
    """CircleCI answered HTTP 404."""


class CircleCIRateLimitedError(CircleCIAPIError):
    """Still rate limited (HTTP 429) after every retry."""


class CircleCIProjectNotFoundError(CircleCINotFoundError):
    """A project's pipeline listing answered 404.

    CircleCI answers 404 both for a deleted project and for one the token can
    no longer see (token rotated to a narrower account, user removed from the
    org). The second is a config problem, so the sync fails and memory is kept.
    """

    def __init__(self, project_slug: str, *, path: str | None = None):
        super().__init__(
            "A project in project_slugs answered HTTP 404: it was deleted, or this token "
            "can no longer see it. Its pipelines are kept; remove it from project_slugs "
            "to forget them.",
            status=404,
            path=path,
        )
        self.project_slug = project_slug


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------
def _make_session(token: str) -> requests.Session:
    """Build a ``requests`` session authenticated with a CircleCI personal API token."""
    session = requests.Session()
    session.headers.update({"Circle-Token": token, "Accept": "application/json"})
    return session


class _ActiveCheckedSession:
    """Calls ``check_active`` before every request, so a host can stop a sync
    whose connection was revoked mid-run."""

    def __init__(self, session: Any, check_active: Callable[[], None]):
        self._session = session
        self._check_active = check_active

    def get(self, *args: Any, **kwargs: Any) -> Any:
        self._check_active()
        return self._session.get(*args, **kwargs)


def _api_get(session: Any, base_url: str, path: str, params: dict | None = None) -> dict:
    """GET an API v2 path and return its JSON, retrying rate limits and transient errors.

    429 and 5xx responses wait for ``Retry-After`` (capped, else exponential
    backoff), as do connection errors and timeouts. Any other error status,
    such as a 404, raises a :class:`CircleCIAPIError` subclass at once so the
    caller decides what it means.
    """
    url = f"{base_url}{path}"
    attempt = 0
    while True:
        last_attempt = attempt == _MAX_RETRIES - 1
        try:
            response = session.get(url, params=params or {}, timeout=_TIMEOUT_SECONDS)
        except (requests.ConnectionError, requests.Timeout) as exc:
            if last_attempt:
                # from None: the requests exception's text carries the full URL.
                raise CircleCIAPIError(
                    f"CircleCI could not be reached ({type(exc).__name__})", path=path
                ) from None
            reason, delay = type(exc).__name__, _retry_after(None, attempt)
        else:
            if response.ok:
                return response.json()
            if response.status_code not in _RETRY_STATUSES or last_attempt:
                raise _api_error(response.status_code, path)
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


def _api_error(status: int, path: str) -> CircleCIAPIError:
    if status in (401, 403):
        error, what = CircleCIAuthError, "rejected the token"
    elif status == 404:
        error, what = CircleCINotFoundError, "found nothing"
    elif status == 429:
        error, what = CircleCIRateLimitedError, "kept rate limiting"
    else:
        error, what = CircleCIAPIError, "answered with an error"
    return error(f"CircleCI API {what} (HTTP {status})", status=status, path=path)


def _retry_after(headers: Any, attempt: int) -> float:
    """Seconds to wait before retrying: Retry-After (capped), else exponential backoff."""
    try:
        delay = float((headers or {}).get("Retry-After"))
    except (TypeError, ValueError):
        return float(2**attempt)
    return min(max(delay, 0.0), _MAX_RETRY_AFTER_SECONDS)


def _paginate(session: Any, base_url: str, path: str, params: dict | None = None) -> Iterator[dict]:
    """Yield ``items`` across pages, following ``next_page_token``.

    Lazy on purpose: the pipeline listing is newest-first, so the sync can stop
    once it passes the cursor without fetching older pages.
    """
    params = dict(params or {})
    seen_tokens: set[str] = set()
    while True:
        data = _api_get(session, base_url, path, params)
        yield from data.get("items") or []
        page_token = data.get("next_page_token")
        if not page_token:
            return
        # A token that comes back again would page forever.
        if page_token in seen_tokens:
            raise CircleCIAPIError("CircleCI pagination did not advance", path=path)
        seen_tokens.add(page_token)
        params["page-token"] = page_token


# ---------------------------------------------------------------------------
# Pipeline → document row
# ---------------------------------------------------------------------------
def _fetch_workflows(
    session: Any, base_url: str, project_slug: str, pipeline_id: str
) -> list[dict]:
    """A pipeline's workflows, each with its ``jobs``; failed jobs also get ``failing_tests``."""
    workflows = list(_paginate(session, base_url, f"/pipeline/{pipeline_id}/workflow"))
    return _fetch_jobs(session, base_url, project_slug, workflows)


def _fetch_jobs(
    session: Any, base_url: str, project_slug: str, workflows: list[dict]
) -> list[dict]:
    """Add ``jobs`` to each workflow, and ``failing_tests`` to each failed job.

    Test results are fetched for failed jobs only. Passing jobs become a status
    line, and build logs are never read.
    """
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
    secrets: Iterable[str] = (),
) -> dict[str, Any]:
    """Flatten a pipeline and its fetched workflows into a document row.

    Document-mode rows are ``{id, title, content, url}``: cognee only turns
    ``title`` and ``content`` into text, so everything worth remembering goes
    into ``content``. Durations and other timestamps are left out, and
    workflows, jobs and tests are sorted, so a finished pipeline's text never
    changes and is never re-cognified. ``secrets`` are masked wherever they
    appear in failure output, on top of the common credential shapes.
    """
    vcs = pipeline.get("vcs") or {}
    ref = vcs.get("branch") or vcs.get("tag")
    title = f"{pipeline.get('project_slug')} pipeline #{pipeline.get('number')}"
    if ref:
        title += f" on {ref}"
    title += f": {_overall_status(pipeline, workflows)}"

    redact = functools.partial(_redact, secrets=tuple(s for s in secrets if s))
    return {
        "id": str(pipeline["id"]),
        "title": title,
        "content": _render_content(
            pipeline, workflows, max_failing_tests, max_message_chars, redact
        ),
        "url": _pipeline_url(pipeline),
        # Hard-delete marker (always False for live pipelines).
        "_deleted": False,
    }


def _overall_status(pipeline: dict, workflows: list[dict]) -> str:
    """One status for the whole pipeline, from the latest run of each workflow.

    The pipeline's own ``state`` only covers config processing (it stays
    ``created`` after every workflow finishes), so it is used only when there
    are no workflows, e.g. ``errored`` for an invalid config. A workflow that
    failed and was rerun green counts as green.
    """
    statuses = {str(w.get("status")) for w in _latest_runs(workflows)}
    if not statuses:
        return pipeline.get("state") or "unknown"
    return next((s for s in _STATUS_PRIORITY if s in statuses), min(statuses))


def _is_finished(pipeline: dict, workflows: list[dict]) -> bool:
    """True once nothing about the pipeline will change, short of a rerun.

    Decided from the workflows, not the pipeline ``state`` (which stays
    ``created`` while they run). A pipeline with no workflows is finished unless
    CircleCI is still setting it up: ``errored`` (bad config) and ``created``
    with every workflow filtered out are both final.
    """
    if pipeline.get("state") in _SETUP_PIPELINE_STATES:
        return False
    return all(w.get("status") in _FINAL_WORKFLOW_STATUSES for w in workflows)


def _runs_by_name(workflows: list[dict]) -> list[tuple[str, list[dict]]]:
    """Workflows grouped by name, sorted by name, each group oldest run first.

    Rerunning a workflow adds a new workflow with the same name to the
    pipeline. Sorting makes the rendered text independent of API order.
    """
    groups: dict[str, list[dict]] = {}
    for workflow in workflows:
        groups.setdefault(str(workflow.get("name")), []).append(workflow)
    return [(name, sorted(groups[name], key=_run_order)) for name in sorted(groups)]


def _latest_runs(workflows: list[dict]) -> list[dict]:
    return [runs[-1] for _, runs in _runs_by_name(workflows)]


def _run_order(workflow: dict) -> tuple[datetime, str]:
    created = workflow.get("created_at")
    return (
        _parse_time(created) if created else datetime.min.replace(tzinfo=timezone.utc),
        str(workflow.get("id")),
    )


def _render_content(
    pipeline: dict,
    workflows: list[dict],
    max_failing_tests: int,
    max_message_chars: int,
    redact: Callable[[str], str],
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
        f"Error ({error.get('type')}): {redact(str(error.get('message')))}"
        for error in pipeline.get("errors") or []
    )

    for name, runs in _runs_by_name(workflows):
        for rerun, workflow in enumerate(runs):
            label = f"Workflow {name}" + (f" (rerun {rerun})" if rerun else "")
            lines += ["", f"{label}: {workflow.get('status')}"]
            jobs = sorted(
                workflow.get("jobs") or [], key=lambda j: (str(j.get("name")), str(j.get("id")))
            )
            for job in jobs:
                lines += _render_job(job, max_failing_tests, max_message_chars, redact)
    return "\n".join(lines)


def _render_job(
    job: dict, max_failing_tests: int, max_message_chars: int, redact: Callable[[str], str]
) -> list[str]:
    status_line = f"- {job.get('name')}: {job.get('status')}"
    if "failing_tests" not in job:
        return [status_line]

    failing = sorted(job["failing_tests"], key=lambda t: (_test_name(t), t.get("message") or ""))
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
        message = redact(_message_tail(test.get("message") or "", max_message_chars))
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


def _redact(text: str, secrets: tuple[str, ...] = ()) -> str:
    """Mask ``secrets`` (e.g. the sync's own token) and common credential shapes."""
    for secret in secrets:
        text = text.replace(secret, _REDACTED)
    for pattern, replacement in _SECRET_PATTERNS:
        text = pattern.sub(replacement, text)
    return text


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
    rerun_window_days: float = DEFAULT_RERUN_WINDOW_DAYS,
    max_failing_tests: int = DEFAULT_MAX_FAILING_TESTS,
    max_message_chars: int = DEFAULT_MAX_MESSAGE_CHARS,
    secrets: Iterable[str] = (),
    stats: dict[str, int] | None = None,
    now: datetime | None = None,
) -> Iterator[dict[str, Any]]:
    """Yield a row for every new pipeline, again for each one that has since
    finished or been rerun, and a ``_deleted`` row for each pipeline of a
    project dropped from ``project_slugs``.

    ``state`` keeps one entry per project slug under ``state["projects"]``:

    * ``cursor``: the newest pipeline ``created_at`` seen so far.
    * ``branch``: the branch filter the cursor belongs to. Changing the filter
      resets the cursor, since the old one would skip the new listing's pipelines.
    * ``pending``: ids of pipelines that were unfinished at the last sync.
    * ``recent``: finished pipelines still inside the rerun window, with the
      ids of their workflows.
    * ``known_ids``: every pipeline id ingested for the project, for deletes.

    Each run first re-checks the pending pipelines and emits a row only for
    those that have finished, so their final status lands without re-ingesting
    every in-between state. It then lists the workflows of each recent
    pipeline and re-emits the ones that gained a workflow (a rerun). Last, it
    pages the newest-first pipeline listing and stops at the cursor.

    A project is forgotten (all its pipelines hard-deleted) only when its slug
    is dropped from ``project_slugs``. A project whose listing answers 404
    raises :class:`CircleCIProjectNotFoundError`, because CircleCI answers the
    same for a project the token can no longer see. Pipelines that age out of
    CircleCI's retention are kept, since CircleCI never deletes them on purpose.

    ``stats`` (when given) receives count-only diagnostics: pipelines
    ``scanned`` and ``skipped`` (re-checked, nothing to emit), rows
    ``deleted``, and ``failed`` / ``failed_rate_limit`` when the run fails.
    """
    stats = {} if stats is None else stats
    for key in _STAT_KEYS:
        stats.setdefault(key, 0)
    to_row = functools.partial(
        _pipeline_to_row,
        max_failing_tests=max_failing_tests,
        max_message_chars=max_message_chars,
        secrets=tuple(secrets),
    )
    try:
        projects = state.setdefault("projects", {})
        for slug in [s for s in projects if s not in project_slugs]:
            yield from _forget_project(slug, projects.pop(slug), stats)

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
                rerun_window=timedelta(days=rerun_window_days),
                now=now or datetime.now(timezone.utc),
                stats=stats,
            )
    except CircleCIRateLimitedError:
        stats["failed"] = stats["failed_rate_limit"] = 1
        raise
    except Exception:
        stats["failed"] = 1
        raise


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
    rerun_window: timedelta,
    now: datetime,
    stats: dict[str, int],
) -> Iterator[dict[str, Any]]:
    params = {"branch": branch} if branch else {}
    listing = _paginate(session, base_url, f"/project/{slug}/pipeline", params)
    # Fetch the first page before anything else, so a project that 404s fails
    # the run before any of its pipelines is re-checked or its state touched.
    try:
        first = next(listing, None)
    except CircleCINotFoundError as exc:
        raise CircleCIProjectNotFoundError(slug, path=exc.path) from None

    cursor: str | None = project_state.get("cursor")
    if cursor is not None and project_state.get("branch") != branch:
        logger.info("CircleCI %s: branch filter changed, listing from the start.", slug)
        cursor = None
    known_ids: set[str] = set(project_state.get("known_ids", []))
    still_pending: list[str] = []
    recent: dict[str, dict] = {}
    handled: set[str] = set()
    new = finished = rerun = 0

    def settle(pipeline: dict, workflows: list[dict]) -> None:
        """File a pipeline under pending or recent for the next sync."""
        pipeline_id = pipeline["id"]
        handled.add(pipeline_id)
        if not _is_finished(pipeline, workflows):
            still_pending.append(pipeline_id)
        elif now - _parse_time(pipeline["created_at"]) < rerun_window:
            recent[pipeline_id] = {
                "created_at": pipeline["created_at"],
                "workflows": _workflow_ids(workflows),
            }

    # 1. Pipelines unfinished at the last sync: emit again only once finished.
    for pipeline_id in project_state.get("pending", []):
        try:
            pipeline = _api_get(session, base_url, f"/pipeline/{pipeline_id}")
        except CircleCINotFoundError:
            # Kept in memory, like any other pipeline CircleCI no longer has.
            logger.info("CircleCI %s: pending pipeline %s is gone, dropping it.", slug, pipeline_id)
            stats["skipped"] += 1
            continue

        stats["scanned"] += 1
        workflows = _fetch_workflows(session, base_url, slug, pipeline_id)
        if _is_finished(pipeline, workflows):
            yield to_row(pipeline, workflows)
            finished += 1
            settle(pipeline, workflows)
        elif now - _parse_time(pipeline["created_at"]) > pending_timeout:
            logger.warning(
                "CircleCI %s: pipeline #%s still unfinished after %s, no longer re-checking it.",
                slug,
                pipeline.get("number"),
                pending_timeout,
            )
            stats["skipped"] += 1
            handled.add(pipeline_id)
        else:
            stats["skipped"] += 1
            settle(pipeline, workflows)

    # 2. Finished pipelines inside the rerun window: one workflow listing each,
    #    and a full re-render only for those that gained a workflow.
    for pipeline_id, seen in project_state.get("recent", {}).items():
        if now - _parse_time(seen["created_at"]) >= rerun_window:
            continue
        try:
            workflows = list(_paginate(session, base_url, f"/pipeline/{pipeline_id}/workflow"))
        except CircleCINotFoundError:
            stats["skipped"] += 1
            continue
        stats["scanned"] += 1
        if _workflow_ids(workflows) == seen["workflows"]:
            stats["skipped"] += 1
            recent[pipeline_id] = seen
            handled.add(pipeline_id)
            continue

        pipeline = _api_get(session, base_url, f"/pipeline/{pipeline_id}")
        workflows = _fetch_jobs(session, base_url, slug, workflows)
        yield to_row(pipeline, workflows)
        rerun += 1
        settle(pipeline, workflows)

    # 3. Pipelines created since the cursor. The listing is newest-first, so stop
    #    at the first one at or before the cursor (later pages are never fetched).
    #    CircleCI's API reference does not state this order; it is what the API
    #    has returned in every recorded response (tests/fixtures/pipelines.json)
    #    and how the CircleCI web app pages through it. Were it ever to change,
    #    a run would stop early and miss pipelines, never duplicate them.
    newest = cursor
    pipelines = itertools.chain([first], listing) if first is not None else iter(())
    for seen_count, pipeline in enumerate(pipelines):
        created = _parse_time(pipeline["created_at"])
        if cursor is not None and created <= _parse_time(cursor):
            break
        if (
            cursor is None
            and max_initial_pipelines is not None
            and seen_count >= max_initial_pipelines
        ):
            break
        if newest is None or created > _parse_time(newest):
            newest = pipeline["created_at"]
        if pipeline["id"] in handled:
            # Already re-checked above (the cursor was reset by a branch change).
            continue

        stats["scanned"] += 1
        workflows = _fetch_workflows(session, base_url, slug, pipeline["id"])
        yield to_row(pipeline, workflows)
        new += 1
        known_ids.add(pipeline["id"])
        settle(pipeline, workflows)

    project_state.update(
        cursor=newest,
        branch=branch,
        pending=still_pending,
        recent=recent,
        known_ids=sorted(known_ids),
    )
    logger.info(
        "CircleCI %s: %d new pipeline(s), %d finished and %d rerun since last sync, "
        "%d still pending.",
        slug,
        new,
        finished,
        rerun,
        len(still_pending),
    )


def _workflow_ids(workflows: list[dict]) -> list[str]:
    """A pipeline's workflow ids: a rerun adds one, so a changed list means a rerun."""
    return sorted(str(w["id"]) for w in workflows)


def _forget_project(
    slug: str, project_state: dict, stats: dict[str, int]
) -> Iterator[dict[str, Any]]:
    """Hard-delete every pipeline ingested for a project dropped from project_slugs."""
    known_ids = project_state.get("known_ids", [])
    logger.warning(
        "CircleCI %s: no longer in project_slugs, forgetting its %d pipeline(s).",
        slug,
        len(known_ids),
    )
    for pipeline_id in known_ids:
        stats["deleted"] += 1
        yield _deleted_row(pipeline_id)


def _deleted_row(pipeline_id: str) -> dict[str, Any]:
    """A minimal row that tells dlt to hard-delete a pipeline by id."""
    return {"id": str(pipeline_id), "_deleted": True}


def _parse_time(timestamp: str) -> datetime:
    # Parsed rather than compared as strings: as text, "21:33:18Z" sorts after
    # "21:33:18.514Z" even though it is earlier. Python 3.10's fromisoformat
    # does not accept the "Z" suffix.
    return datetime.fromisoformat(timestamp.replace("Z", "+00:00"))


def circleci_source(
    *,
    project_slugs: list[str],
    token: str | None = None,
    resource_name: str = CIRCLECI_TABLE_NAME,
    branch: str | None = None,
    base_url: str = DEFAULT_BASE_URL,
    max_initial_pipelines: int | None = DEFAULT_MAX_INITIAL_PIPELINES,
    pending_timeout_days: float = DEFAULT_PENDING_TIMEOUT_DAYS,
    rerun_window_days: float = DEFAULT_RERUN_WINDOW_DAYS,
    max_failing_tests: int = DEFAULT_MAX_FAILING_TESTS,
    max_message_chars: int = DEFAULT_MAX_MESSAGE_CHARS,
    check_active: Callable[[], None] | None = None,
    service: Any = None,
    session: Any = None,
):
    """Return a ``dlt`` resource that yields CircleCI pipelines for ``cognee.remember``.

    Hand it to ``cognee.remember(..., primary_key="id", write_disposition="merge",
    max_rows_per_table=0)``. ``merge`` matters: cognee defaults to ``replace``,
    which would forget every pipeline not emitted on that run.

    Args:
        project_slugs: Projects to sync, e.g. ``["gh/org/repo"]`` or
            ``["circleci/<org-id>/<project-id>"]`` for GitHub App projects. A
            project dropped from this list is forgotten on the next sync.
        token: CircleCI personal API token. Required unless ``service`` is given.
        resource_name: Stable dlt resource name, also the staging table and the
            key of the incremental state. Every CircleCI source syncing into one
            dataset needs its own: two sources sharing a name share state, so
            each would see the other's projects as dropped and forget them. A
            source whose name changes starts over, and documents it synced
            under the old name are not cleaned up.
        branch: Only sync pipelines on this branch. Changing it re-lists each
            project from the newest pipeline.
        base_url: API base URL.
        max_initial_pipelines: Pipelines to ingest on a project's first sync
            (most recent first). ``None`` ingests its whole history.
        pending_timeout_days: Stop re-checking a pipeline that is still
            unfinished after this many days.
        rerun_window_days: Watch finished pipelines this many days old or
            younger for reruns, at one request per pipeline per sync. ``0``
            turns it off.
        max_failing_tests: Failing tests kept per failed job.
        max_message_chars: Characters kept from the end of each failure message.
        check_active: Optional host authorization checkpoint, called before
            each request and before each row is handed to dlt.
        service: Pre-built ``requests``-like session (an injection point for
            tests and hosts). When omitted, one is built from the token.
        session: Old name of ``service``.

    Returns:
        A ``dlt`` resource named ``resource_name`` with ``primary_key="id"``,
        ``write_disposition="merge"`` and an ``_deleted`` hard-delete column,
        tagged as a ``circleci`` document source. Its ``cognee_sync_stats``
        holds the last run's counts.
    """
    if getattr(dlt_utils, "DOCUMENT_SYNC_VERSION", 0) < 1:
        raise RuntimeError(
            "CircleCI sync requires a cognee build with table-scoped document cleanup "
            "(DOCUMENT_SYNC_VERSION >= 1, cognee 1.6.1). Upgrade cognee before syncing."
        )
    if isinstance(project_slugs, str) or not project_slugs:
        raise ValueError('project_slugs must be a non-empty list, e.g. ["gh/org/repo"].')
    if not resource_name:
        raise ValueError("resource_name must be a non-empty string.")
    if service is not None and session is not None:
        raise ValueError("Pass service or session (its old name), not both.")
    service = service if service is not None else session
    if service is None and not token:
        raise ValueError("CircleCI API token required: pass token=.")
    base_url = base_url.rstrip("/")
    slugs = list(project_slugs)
    stats: dict[str, int] = {}

    @dlt.resource(
        name=resource_name,
        primary_key="id",
        write_disposition="merge",
        # _deleted is a boolean hard-delete marker: rows where it is True are
        # removed from the dlt destination on merge, and cognee's orphan_cleanup
        # then forgets them from the graph and vector stores.
        columns={"_deleted": {"data_type": "bool", "hard_delete": True}},
    )
    def circleci_pipelines():
        stats.clear()
        stats.update(dict.fromkeys(_STAT_KEYS, 0))
        client = service if service is not None else _make_session(token)
        if check_active is not None:
            client = _ActiveCheckedSession(client, check_active)
        yield from dlt_utils.guarded_rows(
            sync_pipelines(
                client,
                base_url,
                dlt.current.resource_state(),
                project_slugs=slugs,
                branch=branch,
                max_initial_pipelines=max_initial_pipelines,
                pending_timeout_days=pending_timeout_days,
                rerun_window_days=rerun_window_days,
                max_failing_tests=max_failing_tests,
                max_message_chars=max_message_chars,
                secrets=(token,) if token else (),
                stats=stats,
            ),
            check_active,
        )

    resource = circleci_pipelines()
    # Opt into cognee's document path: each row (id/title/content/url) becomes a
    # text document that goes through cognify. resolve_dlt_sources reads this
    # marker; it never imports this connector.
    setattr(resource, DOCUMENT_SOURCE_ATTR, CIRCLECI_SOURCE_NAME)
    # Gives this source its own dlt pipeline (and so its own incremental state)
    # per dataset, instead of the one cognee shares between all dlt sources.
    setattr(resource, dlt_utils.PIPELINE_SCOPE_ATTR, resource_name)
    # Host-readable diagnostics: counts only, never slugs or failure text.
    resource.cognee_sync_stats = stats
    return resource
