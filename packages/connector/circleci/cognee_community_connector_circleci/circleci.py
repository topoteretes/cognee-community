"""dlt source for CircleCI pipeline executions (incremental sync + forget-on-delete).

Fetches CircleCI pipeline, workflow, and job outcomes via the CircleCI v2 API,
then yields them as a dlt resource for cognee's ingestion pipeline.

Unlike the relational dlt path (SQL/CSV), pipeline executions are ingested as
*normal documents*: the source declares ``cognee_document_source = "circleci"``, so
``resolve_dlt_sources`` tags each row ``external_metadata["source"] = "circleci"``
(not ``"dlt"``). ``is_dlt_sourced`` therefore returns False and each execution
flows through the standard cognify entity-extraction pipeline — the right
treatment for prose — instead of the deterministic dlt-row schema-context path.

The source uses incremental sync: ``created_at`` cursor with an overlap window
(default 5 minutes) to catch late-updating jobs. Pipelines still running at sync
time get stored in dlt state and re-checked on the next run so their final
status lands.

Deletion: ``write_disposition="replace"`` per pipeline, so each sync produces
the complete current set of that pipeline's executions; anything dropped from
the listing gets cleaned up via orphan cleanup. Retention-expiry is treated as
a "soft" scenario (documented limitation) rather than confirmed deletion.
"""

from __future__ import annotations

import os
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Iterator

from cognee.shared.logging_utils import get_logger
from cognee.tasks.ingestion.dlt_utils import DOCUMENT_SOURCE_ATTR

logger = get_logger("circleci_connector")

# dlt resource / staging-table name for CircleCI pipeline executions.
CIRCLECI_TABLE_NAME = "circleci_pipelines"
CIRCLECI_SOURCE_NAME = "circleci"

# Base URL for the CircleCI v2 REST API.
_API_BASE = "https://circleci.com/api/v2"

# Retry budget for rate-limited / transient CircleCI API responses.
_MAX_RETRIES = 5

# Overlap window (minutes) added to the incremental cursor to catch late-updating jobs.
_OVERLAP_WINDOW_MINUTES = 5

# Default page size for paginated endpoints.
_PAGE_SIZE = 50

_EXTRA_HINT = (
    'The CircleCI connector requires the "circleci" extra: pip install "cognee[circleci]" '
    "(provides dlt and requests)."
)


def circleci_source(
    api_token: str | None = None,
    project_slugs: list[str] | None = None,
    branch: str | None = None,
    org_slug: str | None = None,
    client: Any = None,
):
    """Create a dlt source that yields CircleCI pipeline executions as documents.

    Args:
        api_token: CircleCI personal API token. Falls back to ``CIRCLECI_API_TOKEN``.
        project_slugs: Restrict ingestion to these project slugs
            (e.g. ``["gh/your-org/your-repo"]``). When omitted, pipelines from
            ``org_slug`` (or all followed projects when ``org_slug`` is also
            omitted) are returned.
        branch: Optional branch filter. Applies only to per-project listing.
        org_slug: Organization slug (e.g. ``"gh/your-org"``) used by the
            org-wide pipeline listing when ``project_slugs`` is not provided.
        client: Pre-built HTTP client callable (test-injection point); when
            omitted, a requests-based client is built from the token above.

    Returns:
        A dlt source suitable for ``cognee.add(...)`` / ``cognee.remember(...)``.
    """
    try:
        import dlt
    except ImportError as exc:
        raise ImportError(_EXTRA_HINT) from exc

    if client is None:
        try:
            import requests
        except ImportError as exc:
            raise ImportError(_EXTRA_HINT) from exc

        resolved_token = api_token or os.environ.get("CIRCLECI_API_TOKEN")
        if not resolved_token:
            raise ValueError(
                "CircleCI API token required: pass api_token= or set CIRCLECI_API_TOKEN."
            )

        session = requests.Session()
        session.headers.update({"Circle-Token": resolved_token})

        def _api_request(method: str, path: str, **kwargs: Any) -> Any:
            """Call a CircleCI v2 API method, retrying rate-limit / transient errors."""
            url = f"{_API_BASE}{path}"
            for attempt in range(_MAX_RETRIES):
                response = session.request(method, url, **kwargs)
                if response.status_code == 429:
                    retry_after = int(response.headers.get("retry-after", 2**attempt))
                    time.sleep(retry_after)
                    continue
                if response.status_code >= 500 and attempt < _MAX_RETRIES - 1:
                    time.sleep(2**attempt)
                    continue
                response.raise_for_status()
                return response.json()
            raise RuntimeError(
                f"CircleCI API: {method} {path} failed after {_MAX_RETRIES} retries."
            )

        client = _api_request

    @dlt.resource(
        name=CIRCLECI_TABLE_NAME,
        primary_key="id",
        write_disposition="replace",
    )
    def circleci_pipelines() -> Iterator[dict[str, Any]]:
        """Yield one document per pipeline execution.

        Full-snapshot sync: each run replaces staging with exactly the
        executions currently visible. Pipelines that disappear from the API
        listing (retention expiry / deletion) fall out of staging and cognee's
        orphan_cleanup then forgets them. Unchanged executions keep a stable
        content-hash ``data_id`` and are not re-ingested / re-cognified.

        A render error is NOT swallowed: because staging is authoritative
        (replace), an execution missing from a partial snapshot would be
        forgotten as if deleted. Letting the error abort the run leaves
        staging — and memory — untouched, which is the safe failure mode.
        """
        # Access dlt's incremental state through the resource decorator's state.
        # dlt injects ``dlt.current.resource_state()`` at runtime.
        import dlt as _runtime_dlt

        state = _runtime_dlt.current.resource_state()
        last_created_at = state.get("last_created_at")
        if last_created_at:
            # Subtract the overlap window so late-updating jobs are not missed.
            cursor_dt = (
                datetime.fromisoformat(last_created_at)
                - timedelta(minutes=_OVERLAP_WINDOW_MINUTES)
            )
            cursor_iso = cursor_dt.astimezone(timezone.utc).isoformat()
        else:
            cursor_iso = None

        count = 0
        for execution in _iter_pipeline_executions(
            client, project_slugs, branch, org_slug, cursor_iso
        ):
            document = _execution_to_document(client, execution)
            count += 1
            yield document
            # Advance the cursor to the latest execution we've seen.
            created_at = execution.get("created_at")
            if created_at:
                current = state.get("last_created_at")
                if current is None or created_at > current:
                    state["last_created_at"] = created_at

        logger.info("CircleCI: synced %d execution(s).", count)

    @dlt.source(name=CIRCLECI_SOURCE_NAME)
    def _circleci() -> Any:
        return circleci_pipelines

    source = _circleci()
    # Opt into the document ingestion path (execution -> text document -> cognify).
    # resolve_dlt_sources reads this marker; it never imports this connector.
    setattr(source, DOCUMENT_SOURCE_ATTR, CIRCLECI_SOURCE_NAME)
    return source


# ---------------------------------------------------------------------
# CircleCI API helpers (module-private)
# ---------------------------------------------------------------------


def _iter_pipeline_executions(
    client: Any,
    project_slugs: list[str] | None,
    branch: str | None,
    org_slug: str | None,
    cursor_iso: str | None,
) -> Iterable[dict[str, Any]]:
    """Yield pipeline executions from the CircleCI v2 API.

    When ``project_slugs`` is provided, iterates ``GET /project/{slug}/pipeline``
    for each slug. Otherwise uses ``GET /pipeline`` with ``org-slug`` (or the
    user's followed projects when ``org_slug`` is also omitted).

    Only executions whose ``created_at`` is >= ``cursor_iso`` are yielded.
    """
    if project_slugs:
        for slug in project_slugs:
            params: dict[str, Any] = {"per-page": _PAGE_SIZE}
            if branch:
                params["branch"] = branch
            yield from _paginate(
                client,
                f"/project/{slug}/pipeline",
                params,
                cursor_iso,
            )
    else:
        params = {"per-page": _PAGE_SIZE}
        if org_slug:
            params["org-slug"] = org_slug
        else:
            params["mine"] = True
        yield from _paginate(client, "/pipeline", params, cursor_iso)


def _paginate(
    client: Any,
    path: str,
    params: dict[str, Any],
    cursor_iso: str | None,
) -> Iterable[dict[str, Any]]:
    """Follow ``next_page_token`` until exhausted, yielding each pipeline item."""
    next_page_token: str | None = None
    while True:
        call_params = dict(params)
        if next_page_token:
            call_params["page-token"] = next_page_token
        data = client("GET", path, params=call_params)
        items = data.get("items", [])
        for item in items:
            created_at = item.get("created_at")
            if cursor_iso and created_at and created_at < cursor_iso:
                continue
            yield item
        next_page_token = data.get("next_page_token")
        if not next_page_token:
            break


def _execution_to_document(client: Any, execution: dict[str, Any]) -> dict[str, Any]:
    """Transform a raw pipeline execution into a cognee document row.

    Fetches workflows and jobs for the pipeline; for failed jobs, pulls
    bounded failure output from the tests endpoint. Assembles a single
    document per pipeline execution.
    """
    pipeline_id = execution.get("id")
    workflows = []
    if pipeline_id:
        try:
            wf_data = client("GET", f"/pipeline/{pipeline_id}/workflow")
            for wf in wf_data.get("items", []):
                wf_id = wf.get("id")
                jobs = []
                if wf_id:
                    try:
                        jobs_data = client("GET", f"/workflow/{wf_id}/job")
                        for job in jobs_data.get("items", []):
                            job_record = _extract_job(client, execution, job)
                            jobs.append(job_record)
                    except Exception:  # noqa: BLE001 — best-effort enrichment
                        logger.warning("Failed to fetch jobs for workflow %s", wf_id)
                workflows.append(
                    {
                        "id": wf.get("id"),
                        "name": wf.get("name"),
                        "status": wf.get("status"),
                        "created_at": wf.get("created_at"),
                        "stopped_at": wf.get("stopped_at"),
                        "jobs": jobs,
                    }
                )
        except Exception:  # noqa: BLE001 — best-effort enrichment
            logger.warning("Failed to fetch workflows for pipeline %s", pipeline_id)

    trigger = execution.get("trigger", {}) or {}
    actor = trigger.get("actor", {}) or {}
    commit = trigger.get("commit", {}) or {}

    # Build a prose-friendly text body that will be cognified.
    body_sections: list[str] = []
    body_sections.append(
        f"CircleCI pipeline #{execution.get('number')} "
        f"({execution.get('state', 'unknown')})"
    )
    body_sections.append(f"Project: {execution.get('project_slug', 'unknown')}")
    body_sections.append(f"Branch: {execution.get('vcs', {}).get('branch', 'unknown')}")
    body_sections.append(
        f"Commit: {commit.get('subject', '')} "
        f"({commit.get('sha', 'unknown')[:7]})"
    )
    body_sections.append(f"Author: {actor.get('login', 'unknown')}")
    body_sections.append(f"Created: {execution.get('created_at', 'unknown')}")

    for wf in workflows:
        body_sections.append(f"\nWorkflow: {wf['name']} — {wf['status']}")
        for job in wf.get("jobs", []):
            status = job.get("status", "unknown")
            body_sections.append(f"  Job: {job.get('name')} — {status}")
            if status == "failed" and job.get("tests"):
                for test in job["tests"]:
                    body_sections.append(
                        f"    FAIL {test.get('name')} "
                        f"({test.get('file', 'unknown')}): "
                        f"{test.get('message', 'no message')}"
                    )

    document: dict[str, Any] = {
        "id": pipeline_id,
        "pipeline_number": execution.get("number"),
        "project_slug": execution.get("project_slug"),
        "state": execution.get("state"),
        "created_at": execution.get("created_at"),
        "updated_at": execution.get("updated_at"),
        "branch": execution.get("vcs", {}).get("branch"),
        "commit_sha": commit.get("sha"),
        "commit_subject": commit.get("subject"),
        "commit_body": commit.get("body"),
        "author_login": actor.get("login"),
        "author_name": actor.get("name"),
        "workflows": workflows,
        "text": "\n".join(body_sections),
        "source": CIRCLECI_SOURCE_NAME,
    }
    return document


def _extract_job(
    client: Any,
    execution: dict[str, Any],
    job: dict[str, Any],
) -> dict[str, Any]:
    """Extract a job record; if the job failed, also pull test failure data."""
    job_record: dict[str, Any] = {
        "id": job.get("id"),
        "name": job.get("name"),
        "status": job.get("status"),
        "job_number": job.get("job_number"),
        "started_at": job.get("started_at"),
        "stopped_at": job.get("stopped_at"),
        "duration_ms": job.get("duration"),
    }

    if job.get("status") == "failed":
        project_slug = execution.get("project_slug")
        job_number = job.get("job_number")
        if project_slug and job_number:
            try:
                # The tests endpoint lives under the project + job-number path.
                # CircleCI v2 exposes it; failure data is structured and bounded.
                tests_data = client(
                    "GET",
                    f"/project/{project_slug}/{job_number}/tests",
                )
                failed_tests = [
                    {
                        "name": t.get("name"),
                        "file": t.get("file"),
                        "result": t.get("result"),
                        "message": t.get("message"),
                    }
                    for t in tests_data.get("items", [])
                    if t.get("result") != "success"
                ]
                job_record["tests"] = failed_tests
            except Exception:  # noqa: BLE001 — best-effort enrichment
                logger.warning(
                    "Failed to fetch tests for job %s of project %s",
                    job_number,
                    project_slug,
                )

    return job_record
