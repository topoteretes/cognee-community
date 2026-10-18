"""Azure Repos connector: syncs a project's pull requests into cognee.

One document per pull request (description, reviewers, linked work items,
commits, review threads) on the document path, so cognify extracts from it.

* Pull requests have no "last updated" field and the API has no "changed
  since" query, so there is no cursor. Each run lists every PR (cheap) and
  fingerprints the list fields. Comments, work item links and anything past
  400 characters of the description don't show up there, so for open PRs and
  ones closed recently it also reads the full PR and its threads. Quiet old PRs
  cost nothing beyond the list.
* The PR list returns active PRs only unless asked for status=all, and cuts
  descriptions at 400 characters, so full text comes from the single-PR call.
* PRs can't be deleted, only abandoned. What disappears is a repo (deleted,
  deselected, no longer visible), and its PRs are tombstoned with it.
* Callers must pass write_disposition="merge". cognee hands it to
  pipeline.run, which overrides the resource hint, and its default is
  "replace", which would forget every PR that didn't change.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from typing import Any
from urllib.parse import quote

import dlt
import requests
from cognee.shared.logging_utils import get_logger
from cognee.tasks.ingestion import dlt_utils
from cognee.tasks.ingestion.dlt_utils import DOCUMENT_SOURCE_ATTR, PIPELINE_SCOPE_ATTR

logger = get_logger("azure_repos_connector")

SOURCE_TAG = "azure_repos"
API_VERSION = "7.1"
_PR_PAGE = 500
_COMMIT_PAGE = 100
_MAX_RETRIES = 5
_TIMEOUT = 60

_VOTES = {
    10: "approved",
    5: "approved with suggestions",
    -5: "waiting for author",
    -10: "rejected",
}
_THREAD_STATUS = {
    "active": "active",
    "pending": "pending",
    "fixed": "resolved",
    "wontFix": "won't fix",
    "byDesign": "by design",
    "closed": "closed",
}


class AzureDevOpsClient:
    """Read-only client for one org, same as the Azure DevOps Boards connector's.
    Azure DevOps rate limits per identity, so both should share one token's budget."""

    def __init__(self, organization_url: str, pat: str, session: Any = None):
        self.organization_url = organization_url.rstrip("/")
        self.session = session or requests.Session()
        self.session.auth = ("", pat)  # PAT is the password, username is ignored
        self.session.headers.update({"Accept": "application/json"})
        self._wait_until = 0.0

    def get(self, path: str, params: dict | None = None) -> dict:
        return self._request("GET", path, params=params).json()

    def get_page(self, path: str, params: dict | None = None) -> tuple[dict, str | None]:
        """For endpoints that page with an x-ms-continuationtoken header."""
        resp = self._request("GET", path, params=params)
        return resp.json(), resp.headers.get("x-ms-continuationtoken")

    def _request(self, method: str, path: str, **kwargs) -> Any:
        url = path if path.startswith("http") else f"{self.organization_url}/{path.lstrip('/')}"
        for attempt in range(_MAX_RETRIES):
            if (delay := self._wait_until - time.monotonic()) > 0:
                time.sleep(delay)
            resp = self.session.request(method, url, timeout=_TIMEOUT, **kwargs)
            status = resp.status_code
            retry = (status == 429 or status >= 500) and attempt < _MAX_RETRIES - 1
            # ADO also sends Retry-After on a 200, as a warning before it throttles.
            wait = _retry_after(resp.headers)
            if retry and wait is None:
                wait = 2.0**attempt
            if wait is not None:
                self._wait_until = time.monotonic() + wait
            if not retry:
                resp.raise_for_status()
                return resp
            logger.warning("Azure DevOps: HTTP %s on %s, retrying in %.1fs", status, path, wait)
        raise AssertionError("unreachable")


def _retry_after(headers: Any) -> float | None:
    try:
        return max(0.0, float(headers["Retry-After"]))
    except (KeyError, TypeError, ValueError):
        return None


def _repos(client: AzureDevOpsClient, project: str) -> list[dict]:
    path = f"{quote(project)}/_apis/git/repositories"
    return client.get(path, {"api-version": API_VERSION}).get("value") or []


def _pull_requests(
    client: AzureDevOpsClient, project: str, repo_id: str, created_after: date | None
) -> Iterator[dict]:
    """Every PR in the repo, any status.

    Pages with $skip until an empty page, not until a short one: the server may
    cap $top below what we ask for, and stopping on a short page would drop PRs.
    """
    path = f"{quote(project)}/_apis/git/repositories/{repo_id}/pullrequests"
    params: dict[str, Any] = {
        "api-version": API_VERSION,
        "searchCriteria.status": "all",  # the default is active only
        "$top": _PR_PAGE,
    }
    if created_after:
        params["searchCriteria.queryTimeRangeType"] = "created"
        params["searchCriteria.minTime"] = f"{created_after.isoformat()}T00:00:00Z"
    skip = 0
    while page := client.get(path, {**params, "$skip": skip}).get("value") or []:
        yield from page
        skip += len(page)


def _pull_request(client: AzureDevOpsClient, project: str, repo_id: str, pr_id: int) -> dict:
    path = f"{quote(project)}/_apis/git/repositories/{repo_id}/pullrequests/{pr_id}"
    return client.get(path, {"api-version": API_VERSION, "includeWorkItemRefs": "true"})


def _threads(client: AzureDevOpsClient, project: str, repo_id: str, pr_id: int) -> list[dict]:
    path = f"{quote(project)}/_apis/git/repositories/{repo_id}/pullRequests/{pr_id}/threads"
    return client.get(path, {"api-version": API_VERSION}).get("value") or []


def _commits(client: AzureDevOpsClient, project: str, repo_id: str, pr_id: int) -> list[dict]:
    repo_path = f"{quote(project)}/_apis/git/repositories/{repo_id}"
    params: dict[str, Any] = {"api-version": API_VERSION, "$top": _COMMIT_PAGE}
    commits: list[dict] = []
    while True:
        page, token = client.get_page(f"{repo_path}/pullRequests/{pr_id}/commits", params)
        commits += page.get("value") or []
        if not token:
            break
        params = {**params, "continuationToken": token}
    for commit in commits:
        if commit.get("commentTruncated"):  # the list cuts long messages
            full = client.get(
                f"{repo_path}/commits/{commit['commitId']}", {"api-version": API_VERSION}
            )
            commit["comment"] = full.get("comment") or commit.get("comment")
    return commits


def _hash(parts: Any) -> str:
    return hashlib.sha256(json.dumps(parts, default=str).encode()).hexdigest()[:16]


def _list_fingerprint(repo: dict, pr: dict) -> str:
    """What a reader could notice changing, from the list call alone."""
    return _hash(
        [
            repo.get("name"),
            pr.get("status"),
            pr.get("title"),
            pr.get("description"),
            pr.get("closedDate"),
            pr.get("isDraft"),
            (pr.get("lastMergeSourceCommit") or {}).get("commitId"),
            sorted((r.get("id") or "", r.get("vote") or 0) for r in pr.get("reviewers") or []),
            sorted(lb.get("name") or "" for lb in pr.get("labels") or []),
        ]
    )


def _detail_fingerprint(full: dict, threads: list[dict]) -> str:
    """What only the single-PR and threads calls show: the untruncated description,
    linked work items and comments."""
    return _hash(
        [
            full.get("description"),
            sorted(str(w.get("id")) for w in full.get("workItemRefs") or []),
            sorted((t.get("id"), t.get("lastUpdatedDate"), t.get("isDeleted")) for t in threads),
        ]
    )


def _branch(ref: str | None) -> str:
    return (ref or "").removeprefix("refs/heads/")


def _name(identity: Any) -> str:
    """Display name only, emails stay out of memory."""
    if isinstance(identity, dict):
        return identity.get("displayName") or ""
    return ""


def _status_line(pr: dict) -> str:
    status = pr.get("status")
    if status == "completed":
        target, source = _branch(pr.get("targetRefName")), _branch(pr.get("sourceRefName"))
        text = f"merged into {target} from {source}"
    elif status == "abandoned":
        text = "abandoned"
    else:
        text = f"open, {_branch(pr.get('sourceRefName'))} into {_branch(pr.get('targetRefName'))}"
    return f"{text} (draft)" if pr.get("isDraft") else text


def _reviewers(pr: dict) -> str:
    out = []
    for r in pr.get("reviewers") or []:
        if r.get("isContainer") and not r.get("vote"):
            continue  # a team added as reviewer that nobody voted for says nothing
        vote = _VOTES.get(r.get("vote") or 0)
        out.append(f"{_name(r)} ({vote})" if vote else _name(r))
    return ", ".join(sorted(filter(None, out)))


def _thread_block(thread: dict) -> str | None:
    if thread.get("isDeleted"):
        return None
    comments = [
        c
        for c in thread.get("comments") or []
        # system comments are "X voted 10" / "ref updated" noise; votes are on the reviewers line
        if c.get("commentType") != "system"
        and not c.get("isDeleted")
        and (c.get("content") or "").strip()
    ]
    if not comments:
        return None
    ctx = thread.get("threadContext") or {}
    where = "General"
    if ctx.get("filePath"):
        line = (ctx.get("rightFileStart") or ctx.get("leftFileStart") or {}).get("line")
        where = f"On {ctx['filePath'].lstrip('/')}" + (f" line {line}" if line else "")
    if status := _THREAD_STATUS.get(thread.get("status") or ""):
        where += f" ({status})"
    lines = [f"{where}:"]
    lines += [f"- {_name(c.get('author')) or 'Someone'}: {c['content'].strip()}" for c in comments]
    return "\n".join(lines)


def _commit_line(commit: dict) -> str:
    message = (commit.get("comment") or "").strip()
    subject, _, body = message.partition("\n")
    author = (commit.get("author") or {}).get("name")
    line = f"- {(commit.get('commitId') or '')[:7]} {subject}" + (f" ({author})" if author else "")
    if body.strip():
        line += "\n" + "\n".join(f"  {b}" for b in body.strip().splitlines())
    return line


def render_pull_request(repo: dict, pr: dict, threads: list[dict], commits: list[dict]) -> str:
    """Leave out anything that changes without a reader noticing (merge ids, iteration
    numbers), so unchanged PRs keep the same content hash."""
    lines: list[str] = []

    def add(label: str, value: Any) -> None:
        if value:
            lines.append(f"{label}: {value}")

    add("Repository", repo.get("name"))
    add("Status", _status_line(pr))
    add("Created by", _name(pr.get("createdBy")))
    add("Created", (pr.get("creationDate") or "")[:10])
    add("Closed by", _name(pr.get("closedBy")))
    add("Closed", (pr.get("closedDate") or "")[:10])
    add("Reviewers", _reviewers(pr))
    add("Labels", ", ".join(sorted(lb["name"] for lb in pr.get("labels") or [] if lb.get("name"))))
    work_items = sorted(
        {int(w["id"]) for w in pr.get("workItemRefs") or [] if str(w.get("id")).isdigit()}
    )
    # Same wording as the Boards connector, so both land on one graph node.
    add("Work items", ", ".join(f"work item #{w}" for w in work_items))

    if description := (pr.get("description") or "").strip():
        lines.append(f"\nDescription:\n{description}")
    if commits:
        lines.append("\nCommits:\n" + "\n".join(map(_commit_line, commits)))
    if discussion := [b for b in map(_thread_block, threads) if b]:
        lines.append("\nReview discussion:\n" + "\n\n".join(discussion))
    return "\n".join(lines).strip()


def _row(repo: dict, pr: dict, content: str, org_url: str, project: str) -> dict[str, Any]:
    pr_id = pr["pullRequestId"]
    web = repo.get("webUrl") or f"{org_url}/{quote(project)}/_git/{quote(repo.get('name') or '')}"
    return {
        "id": f"{repo['id']}/{pr_id}",
        "title": f"Pull request !{pr_id}" + (f": {pr['title']}" if pr.get("title") else ""),
        "content": content,
        "url": f"{web}/pullrequest/{pr_id}",
        "_deleted": False,
    }


@dataclass
class _Scope:
    project: str
    repositories: list[str] = field(default_factory=list)
    include_abandoned: bool = True
    include_commits: bool = True
    created_after: date | None = None
    recheck_closed_days: int | None = 30

    def wants_repo(self, repo: dict) -> bool:
        return not self.repositories or repo.get("name", "").lower() in {
            r.lower() for r in self.repositories
        }

    def warm(self, pr: dict, now: datetime) -> bool:
        """Whether to read threads this run. Comments mostly land on open PRs."""
        if self.recheck_closed_days is None or pr.get("status") == "active":
            return True
        closed = pr.get("closedDate")
        if not closed:
            return True
        closed_at = datetime.fromisoformat(closed.replace("Z", "+00:00"))
        return now - closed_at <= timedelta(days=self.recheck_closed_days)


def sync_pull_requests(
    client: AzureDevOpsClient,
    scope: _Scope,
    state: dict,
    stats: dict | None = None,
    now: datetime | None = None,
) -> Iterator[dict]:
    """Rows for new or changed PRs, then tombstones.

    ``state["fingerprints"]`` maps each PR key to "<list fp>:<detail fp>". The
    detail half is only compared on runs that fetched it, so an old PR going quiet
    doesn't look like a change. State is written at the end only, so a failed run
    is retried from where the last good one left off.
    """
    now = now or datetime.now(UTC)
    known: dict[str, str] = dict(state.get("fingerprints", {}))
    stats = stats if stats is not None else {}
    stats.update(scanned=0, changed=0, removed=0)

    repos = _repos(client, scope.project)
    if not repos and known:
        # A failed listing is far likelier than a project with no repos left.
        logger.warning("Azure Repos: no repositories listed but %d PRs known, skipping", len(known))
        return
    selected = [r for r in repos if scope.wants_repo(r)]
    missing = set(map(str.lower, scope.repositories)) - {r["name"].lower() for r in selected}
    if missing:
        logger.warning("Azure Repos: repositories not found: %s", ", ".join(sorted(missing)))

    seen: set[str] = set()
    for repo in selected:
        prefix = f"{repo['id']}/"
        if repo.get("isDisabled"):
            # Disabled isn't deleted, and its PRs can't be read. Keep what we have.
            seen |= {key for key in known if key.startswith(prefix)}
            continue
        for pr in _pull_requests(client, scope.project, repo["id"], scope.created_after):
            if pr.get("status") == "abandoned" and not scope.include_abandoned:
                continue
            key = prefix + str(pr["pullRequestId"])
            seen.add(key)
            stats["scanned"] += 1
            pr_id = pr["pullRequestId"]
            list_fp = _list_fingerprint(repo, pr)
            old_list_fp, _, old_detail_fp = known.get(key, "").partition(":")
            full: dict | None = None
            threads: list[dict] = []
            detail_fp = None
            if key not in known or scope.warm(pr, now):
                full = _pull_request(client, scope.project, repo["id"], pr_id)
                threads = _threads(client, scope.project, repo["id"], pr_id)
                detail_fp = _detail_fingerprint(full, threads)
            if old_list_fp == list_fp and detail_fp in (None, old_detail_fp):
                continue
            if full is None:
                full = _pull_request(client, scope.project, repo["id"], pr_id)
                threads = _threads(client, scope.project, repo["id"], pr_id)
                detail_fp = _detail_fingerprint(full, threads)
            commits = (
                _commits(client, scope.project, repo["id"], pr_id) if scope.include_commits else []
            )
            known[key] = f"{list_fp}:{detail_fp}"
            stats["changed"] += 1
            content = render_pull_request(repo, full, threads, commits)
            yield _row(repo, full, content, client.organization_url, scope.project)

    for key in sorted(set(known) - seen):
        del known[key]
        stats["removed"] += 1
        yield {"id": key, "_deleted": True}

    state["fingerprints"] = known
    logger.info(
        "Azure Repos: %(scanned)d PRs scanned, %(changed)d changed, %(removed)d removed", stats
    )


def _resource_name(org_url: str, project: str) -> str:
    org = org_url.rstrip("/").rsplit("/", 1)[-1]
    return "azure_repos_" + re.sub(r"[^a-z0-9]+", "_", f"{org}_{project}".lower()).strip("_")


def azure_repos_source(
    *,
    organization: str | None = None,
    project: str,
    pat: str | None = None,
    organization_url: str | None = None,
    repositories: Iterable[str] | None = None,
    include_abandoned: bool = True,
    include_commits: bool = True,
    created_after: date | None = None,
    recheck_closed_days: int | None = 30,
    check_active: Callable[[], None] | None = None,
    client: AzureDevOpsClient | None = None,
):
    """A dlt resource with one row per pull request. Use write_disposition="merge".

    Args:
        organization: Name from ``https://dev.azure.com/<organization>``.
        project: Project name or id.
        pat: Token with Code (Read). Defaults to ``AZURE_DEVOPS_PAT``.
        organization_url: Full URL instead, for Azure DevOps Server or
            ``*.visualstudio.com``.
        repositories: Repository names to sync. All of the project's by default.
        include_abandoned: Keep abandoned PRs. They often explain why something
            wasn't done.
        include_commits: Add each PR's commit messages.
        created_after: Skip PRs created before this date.
        recheck_closed_days: Re-read threads of PRs closed within this many days
            on every run. ``None`` re-reads every PR, which costs one request
            per PR per run.
        check_active: Optional host authorization checkpoint during extraction.
        client: Ready-made client, for tests.
    """
    if getattr(dlt_utils, "DOCUMENT_SYNC_VERSION", 0) < 1:
        raise RuntimeError(
            "Azure Repos sync needs a cognee build with table-scoped document cleanup."
        )
    if client is None:
        url = organization_url or (organization and f"https://dev.azure.com/{organization}")
        token = pat or os.environ.get("AZURE_DEVOPS_PAT")
        if not url or not token:
            raise ValueError(
                "Need organization= (or organization_url=) and pat= (or AZURE_DEVOPS_PAT)."
            )
        client = AzureDevOpsClient(url, token)

    scope = _Scope(
        project,
        list(repositories or []),
        include_abandoned,
        include_commits,
        created_after,
        recheck_closed_days,
    )
    # Named per org and project, and used as the pipeline scope, so two projects
    # (or one project in two datasets) never share fingerprints or a cleanup table.
    name = _resource_name(client.organization_url, project)
    stats: dict[str, int] = {}

    @dlt.resource(
        name=name,
        primary_key="id",
        write_disposition="merge",
        columns={"_deleted": {"data_type": "bool", "hard_delete": True}},
    )
    def azure_repos_pull_requests():
        rows = sync_pull_requests(client, scope, dlt.current.resource_state(), stats)
        yield from dlt_utils.guarded_rows(rows, check_active)

    resource = azure_repos_pull_requests()
    setattr(resource, DOCUMENT_SOURCE_ATTR, SOURCE_TAG)  # route through cognify
    setattr(resource, PIPELINE_SCOPE_ATTR, name)
    resource.cognee_sync_stats = stats  # counts only, never titles or content
    return resource
