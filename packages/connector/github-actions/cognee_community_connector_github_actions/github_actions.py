"""GitHub Actions CI/CD data-source connector for cognee.

Syncs GitHub Actions workflow runs, status, conclusions, and failed jobs
into cognee memory for CI diagnostics and flaky test analysis.
"""

import os
from collections.abc import Iterator
from typing import Any

import requests
from cognee.shared.logging_utils import get_logger
from cognee.tasks.ingestion.dlt_utils import DOCUMENT_SOURCE_ATTR

logger = get_logger("github_actions_connector")

GHA_TABLE_NAME = "github_actions_runs"
GHA_SOURCE_NAME = "github_actions"
GITHUB_API_BASE = "https://api.github.com"


def _headers(token: str | None = None) -> dict[str, str]:
    headers = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "cognee-github-actions-connector/1.0",
    }
    resolved = token or os.environ.get("GITHUB_TOKEN")
    if resolved:
        headers["Authorization"] = f"Bearer {resolved}"
    return headers


def iter_workflow_runs(
    repository: str,
    token: str | None = None,
    branch: str | None = None,
    status: str | None = None,
    limit: int = 30,
    include_jobs: bool = True,
    fetch_fn: Any = None,
) -> Iterator[dict[str, Any]]:
    """Yield parsed GitHub Actions workflow runs and jobs as documents."""
    repo = repository.strip("/")
    _owner, _repo_name = repo.split("/", 1) if "/" in repo else ("unknown", repo)

    def _default_fetch(url: str, params: dict[str, Any] | None = None) -> Any:
        resp = requests.get(url, headers=_headers(token), params=params, timeout=15)
        resp.raise_for_status()
        return resp.json()

    fetcher = fetch_fn or _default_fetch

    runs_url = f"{GITHUB_API_BASE}/repos/{repo}/actions/runs"
    params: dict[str, Any] = {"per_page": min(limit, 100)}
    if branch:
        params["branch"] = branch
    if status:
        params["status"] = status

    try:
        data = fetcher(runs_url, params=params)
    except Exception as exc:
        logger.error("GitHub Actions: failed fetching runs for %s: %s", repo, exc)
        return

    runs = data.get("workflow_runs", [])
    for run in runs[:limit]:
        run_id = run.get("id")
        name = run.get("name", "Workflow")
        head_branch = run.get("head_branch", "")
        head_sha = run.get("head_sha", "")[:8]
        event = run.get("event", "")
        run_status = run.get("status", "")
        conclusion = run.get("conclusion") or run_status
        created_at = run.get("created_at", "")
        updated_at = run.get("updated_at", "")
        html_url = run.get("html_url", "")
        actor = run.get("actor", {}).get("login", "")

        # Fetch jobs if requested
        jobs_summary: list[str] = []
        failed_steps: list[str] = []

        if include_jobs and run_id:
            jobs_url = f"{GITHUB_API_BASE}/repos/{repo}/actions/runs/{run_id}/jobs"
            try:
                jobs_data = fetcher(jobs_url, params=None)
                jobs_list = jobs_data.get("jobs", [])
                for job in jobs_list:
                    j_name = job.get("name", "job")
                    j_conclusion = job.get("conclusion") or job.get("status", "")
                    jobs_summary.append(f"- **Job:** {j_name} (`{j_conclusion}`)")

                    for step in job.get("steps", []):
                        s_name = step.get("name", "")
                        s_conclusion = step.get("conclusion", "")
                        if s_conclusion in ("failure", "timed_out"):
                            failed_steps.append(f"  - Failed Step in {j_name}: `{s_name}` ({s_conclusion})")
            except Exception as exc:
                logger.warning("GitHub Actions: failed fetching jobs for run %s: %s", run_id, exc)

        doc_id = f"gha:run:{repo}:{run_id}"
        lines = [
            f"# GitHub Actions: {repo} - {name} (#{run.get('run_number', '')})",
            f"**Conclusion:** {conclusion.upper()} | **Status:** {run_status}",
            f"**Branch:** {head_branch} | **Commit:** {head_sha} | **Trigger:** {event}",
            f"**Actor:** {actor} | **Run ID:** {run_id}",
            f"**Created:** {created_at} | **Updated:** {updated_at}",
            f"**URL:** {html_url}",
        ]

        if failed_steps:
            lines.append("\n### Diagnostic Failure Steps\n" + "\n".join(failed_steps))

        if jobs_summary:
            lines.append("\n### Workflow Jobs\n" + "\n".join(jobs_summary))

        content = "\n".join(lines)

        yield {
            "id": doc_id,
            "run_id": run_id,
            "repository": repo,
            "workflow_name": name,
            "branch": head_branch,
            "commit_sha": head_sha,
            "event": event,
            "status": run_status,
            "conclusion": conclusion,
            "actor": actor,
            "created_at": created_at,
            "updated_at": updated_at,
            "url": html_url,
            "title": f"CI Run: {repo} - {name} ({conclusion})",
            "content": content,
        }


def github_actions_source(
    repository: str,
    token: str | None = None,
    branch: str | None = None,
    status: str | None = None,
    limit: int = 30,
    include_jobs: bool = True,
    fetch_fn: Any = None,
):
    """Create a dlt source yielding GitHub Actions workflow runs as documents."""
    import dlt

    @dlt.resource(name=GHA_TABLE_NAME, primary_key="id", write_disposition="replace")
    def gha_runs():
        count = 0
        for run_doc in iter_workflow_runs(
            repository=repository,
            token=token,
            branch=branch,
            status=status,
            limit=limit,
            include_jobs=include_jobs,
            fetch_fn=fetch_fn,
        ):
            count += 1
            yield run_doc
        logger.info("GitHub Actions: synced %d workflow run(s).", count)

    @dlt.source(name=GHA_SOURCE_NAME)
    def _github_actions():
        return gha_runs

    source = _github_actions()
    setattr(source, DOCUMENT_SOURCE_ATTR, GHA_SOURCE_NAME)
    return source
