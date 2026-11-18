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
  deselected, no longer visible), and its PRs are tombstoned with it. That
  only happens after a complete listing.
* Each run has a request budget and a budget for time spent waiting on
  Retry-After. When either runs out, or Azure DevOps keeps answering 429 or
  5xx, the run stops cleanly: what's synced stays, nothing is deleted, and the
  next run picks up the rest.
* Callers must pass write_disposition="merge". cognee hands it to
  pipeline.run, which overrides the resource hint, and its default is
  "replace", which would forget every PR that didn't change.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Any
from urllib.parse import quote, urlparse

import dlt
import requests
from cognee.shared.logging_utils import get_logger
from cognee.tasks.ingestion import dlt_utils
from cognee.tasks.ingestion.dlt_utils import DOCUMENT_SOURCE_ATTR, PIPELINE_SCOPE_ATTR

logger = get_logger("azure_repos_connector")

SOURCE_TAG = "azure_repos"
API_VERSION = "7.1"
# our own page size for the PR list; the server may cap it lower, which paging handles
_PR_PAGE = 500
# our own page size for PR commits; the next page comes in x-ms-continuationtoken
_COMMIT_PAGE = 100
# requests per run; listing costs about one per 500 PRs, each open PR costs two
DEFAULT_MAX_REQUESTS = 5_000
# seconds per run we're willing to spend sleeping on Retry-After before stopping cleanly
DEFAULT_MAX_WAIT_SECONDS = 900.0
# attempts per request on 429, 5xx or network errors
_MAX_ATTEMPTS = 5
# our own ceiling on a single Retry-After wait; Azure DevOps doesn't document one
_MAX_RETRY_AFTER = 60.0
_TIMEOUT_SECONDS = 60

# reviewer vote values, from the GitPullRequest reviewers docs
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


class AzureDevOpsError(RuntimeError):
    """Azure DevOps answered with an error or couldn't be reached.

    The message carries the status only. ``url`` holds the request URL, which
    names the organisation and project, for whoever needs to debug it.
    """

    def __init__(self, message: str, status: int | None = None, url: str | None = None):
        super().__init__(message)
        self.status = status
        self.url = url


class AzureDevOpsAuthError(AzureDevOpsError):
    """HTTP 401: the personal access token is wrong or expired."""


class AzureDevOpsAccessError(AzureDevOpsError):
    """HTTP 403: the token is missing a scope this call needs."""


class AzureDevOpsRateLimitedError(AzureDevOpsError):
    """HTTP 429 that didn't clear after retrying."""


class AzureDevOpsServerError(AzureDevOpsError):
    """HTTP 5xx, or no connection, that didn't clear after retrying."""


_ERRORS = {
    401: (AzureDevOpsAuthError, "check the personal access token"),
    403: (AzureDevOpsAccessError, "the token is missing a scope this call needs"),
    429: (AzureDevOpsRateLimitedError, "rate limited"),
}


class AzureDevOpsClient:
    """Read-only client for one org, the same one the Azure DevOps Boards connector
    uses. Azure DevOps rate limits per identity, so both share one token's budget."""

    def __init__(
        self,
        organization_url: str,
        pat: str,
        session: Any = None,
        sleep: Callable[[float], None] | None = None,
    ):
        if not pat:
            raise ValueError("A personal access token is required")
        parsed = urlparse(organization_url or "")
        if parsed.scheme not in ("http", "https") or not parsed.netloc:
            raise ValueError(
                "organization_url must be a full URL, like https://dev.azure.com/my-org"
            )
        self.organization_url = organization_url.rstrip("/")
        self.session = session or requests.Session()
        self.session.auth = ("", pat)  # PAT is the password, username is ignored
        self.session.headers.update({"Accept": "application/json"})
        self._sleep = sleep or time.sleep
        self._wait_until = 0.0
        self.waited = 0.0  # seconds slept on Retry-After and backoff, read by the run budget

    def __repr__(self) -> str:
        return f"AzureDevOpsClient({self.organization_url!r}, pat=<redacted>)"

    def get(self, path: str, params: dict | None = None) -> dict:
        return self.get_page(path, params)[0]

    def get_page(self, path: str, params: dict | None = None) -> tuple[dict, str | None]:
        """For endpoints that page with an x-ms-continuationtoken header."""
        resp, url = self._request("GET", path, params=params)
        return _json(resp, url), resp.headers.get("x-ms-continuationtoken")

    def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        url = path if path.startswith("http") else f"{self.organization_url}/{path.lstrip('/')}"
        attempt = 0
        while True:
            attempt += 1
            if (delay := self._wait_until - time.monotonic()) > 0:
                self._sleep(delay)
                self.waited += delay
            try:
                resp = self.session.request(method, url, timeout=_TIMEOUT_SECONDS, **kwargs)
            except (
                requests.exceptions.MissingSchema,
                requests.exceptions.InvalidSchema,
                requests.exceptions.InvalidURL,
            ) as exc:
                raise ValueError("The Azure DevOps URL is not valid") from exc
            except requests.RequestException as exc:
                if attempt >= _MAX_ATTEMPTS:
                    raise AzureDevOpsServerError(
                        "Azure DevOps could not be reached", url=url
                    ) from exc
                self._wait_until = time.monotonic() + 2.0 ** (attempt - 1)
                continue

            status = resp.status_code
            # ADO also sends Retry-After on a 200, as a warning before it throttles.
            wait = _retry_after(resp.headers)
            retryable = status == 429 or status >= 500
            if retryable and attempt < _MAX_ATTEMPTS:
                wait = wait if wait is not None else 2.0 ** (attempt - 1)
                logger.warning("Azure DevOps: HTTP %s, retrying in %.1fs", status, wait)
            if wait is not None:
                self._wait_until = time.monotonic() + wait
            if retryable and attempt < _MAX_ATTEMPTS:
                continue
            if status >= 500:
                raise AzureDevOpsServerError(
                    f"Azure DevOps returned HTTP {status}: server error", status=status, url=url
                )
            if status >= 400:
                error, hint = _ERRORS.get(status, (AzureDevOpsError, "request failed"))
                raise error(f"Azure DevOps returned HTTP {status}: {hint}", status=status, url=url)
            return resp, url


def _json(resp: Any, url: str) -> dict:
    """A bad or expired token can get a 203 with the HTML sign-in page instead of a 401."""
    content_type = (resp.headers or {}).get("Content-Type", "")
    if resp.status_code == 203 or "text/html" in content_type:
        raise AzureDevOpsAuthError(
            "Azure DevOps returned a sign-in page instead of data: check the personal access token",
            status=resp.status_code,
            url=url,
        )
    try:
        return resp.json()
    except ValueError as exc:
        raise AzureDevOpsError(
            "Azure DevOps returned something that isn't JSON", status=resp.status_code, url=url
        ) from exc


def _retry_after(headers: Any) -> float | None:
    try:
        return min(_MAX_RETRY_AFTER, max(0.0, float(headers["Retry-After"])))
    except (KeyError, TypeError, ValueError):
        return None


class _CutShortError(Exception):
    """Stop this run here and keep what's done. ``reason`` ends up in the stats."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


class _Calls:
    """Every request of one run goes through here: host check, then the request
    budget and the time budget. Running out of either, or a 429 or 5xx that won't
    clear, ends the run cleanly instead of losing it."""

    def __init__(
        self,
        service: Any,
        max_requests: int,
        max_wait_seconds: float,
        check_active: Callable[[], None] | None,
    ):
        self.service = service
        self.max_requests = max_requests
        self.max_wait_seconds = max_wait_seconds
        self.check_active = check_active
        self.made = 0
        self.waited_at_start = getattr(service, "waited", 0.0)

    def get(self, path: str, params: dict | None = None) -> dict:
        return self._call(self.service.get, path, params)

    def get_page(self, path: str, params: dict | None = None) -> tuple[dict, str | None]:
        return self._call(self.service.get_page, path, params)

    def _call(self, method: Callable[..., Any], *args: Any) -> Any:
        if self.check_active is not None:
            self.check_active()
        if getattr(self.service, "waited", 0.0) - self.waited_at_start >= self.max_wait_seconds:
            raise _CutShortError("slow_down")
        if self.made >= self.max_requests:
            raise _CutShortError("budget")
        self.made += 1
        try:
            return method(*args)
        except AzureDevOpsRateLimitedError:
            raise _CutShortError("rate_limit") from None
        except AzureDevOpsServerError:
            raise _CutShortError("server_error") from None


def _repos(calls: _Calls, project: str) -> list[dict]:
    path = f"{quote(project)}/_apis/git/repositories"
    return calls.get(path, {"api-version": API_VERSION}).get("value") or []


def _pull_requests(
    calls: _Calls, project: str, repo_id: str, created_after: date | None
) -> Iterator[dict]:
    """Every PR in the repo, any status.

    Pages with $skip until an empty page, not until a short one: the server may
    cap $top below what we ask for, and stopping on a short page would drop PRs.
    A server that ignores $skip would loop forever, so a repeated page raises.
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
    previous: list[int] = []
    while page := calls.get(path, {**params, "$skip": skip}).get("value") or []:
        ids = [pr.get("pullRequestId") for pr in page]
        if ids == previous:
            raise AzureDevOpsError("The pull request list returned the same page twice")
        previous = ids
        yield from page
        skip += len(page)


def _pull_request(calls: _Calls, project: str, repo_id: str, pr_id: int) -> dict:
    path = f"{quote(project)}/_apis/git/repositories/{repo_id}/pullrequests/{pr_id}"
    return calls.get(path, {"api-version": API_VERSION, "includeWorkItemRefs": "true"})


def _threads(calls: _Calls, project: str, repo_id: str, pr_id: int) -> list[dict]:
    path = f"{quote(project)}/_apis/git/repositories/{repo_id}/pullRequests/{pr_id}/threads"
    return calls.get(path, {"api-version": API_VERSION}).get("value") or []


def _commits(calls: _Calls, project: str, repo_id: str, pr_id: int) -> list[dict]:
    repo_path = f"{quote(project)}/_apis/git/repositories/{repo_id}"
    params: dict[str, Any] = {"api-version": API_VERSION, "$top": _COMMIT_PAGE}
    commits: list[dict] = []
    seen: set[str] = set()
    while True:
        page, token = calls.get_page(f"{repo_path}/pullRequests/{pr_id}/commits", params)
        commits += page.get("value") or []
        if not token:
            break
        if token in seen:
            raise AzureDevOpsError("The commit list returned the same continuation token twice")
        seen.add(token)
        params = {**params, "continuationToken": token}
    for commit in commits:
        if commit.get("commentTruncated"):  # the list cuts long messages
            full = calls.get(
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
    comments = sorted(
        (
            c
            for c in thread.get("comments") or []
            # system comments ("X voted 10", "ref updated") are noise; votes have their own line
            if c.get("commentType") != "system"
            and not c.get("isDeleted")
            and (c.get("content") or "").strip()
        ),
        # Comment ids grow in posting order. Dates can't be compared as strings:
        # Azure trims trailing zeros from the fraction, so ".1Z" sorts before ".12Z".
        key=lambda c: c.get("id") or 0,
    )
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


def _commit_order(commit: dict) -> tuple:
    date_text = (commit.get("author") or {}).get("date")
    try:
        when = _parse_time(date_text).timestamp() if date_text else 0.0
    except ValueError:
        when = 0.0
    return (when, commit.get("commitId") or "")


def render_pull_request(repo: dict, pr: dict, threads: list[dict], commits: list[dict]) -> str:
    """Leave out anything that changes without a reader noticing (merge ids, iteration
    numbers) and sort every collection, so unchanged PRs keep the same content hash."""
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
    ordered_commits = sorted(commits, key=_commit_order)
    if ordered_commits:
        lines.append("\nCommits:\n" + "\n".join(map(_commit_line, ordered_commits)))
    ordered_threads = sorted(threads, key=lambda t: t.get("id") or 0)
    if discussion := [b for b in map(_thread_block, ordered_threads) if b]:
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


def _parse_time(value: str) -> datetime:
    """Azure DevOps sends anywhere from 1 to 7 fractional digits. Python 3.10 only
    parses 3 or 6, so pad or cut them to 6 first."""
    value = re.sub(r"\.(\d+)", lambda m: "." + (m.group(1) + "000000")[:6], value)
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


@dataclass
class _Scope:
    project: str
    repositories: list[str] = field(default_factory=list)
    include_abandoned: bool = True
    include_commits: bool = True
    created_after: date | None = None
    recheck_closed_days: int | None = 30

    def __post_init__(self) -> None:
        # Normalised so cosmetic differences (" Shop-API", duplicates, 1 vs True)
        # don't look like a different selection.
        self.repositories = sorted({r.strip().lower() for r in self.repositories if r.strip()})
        self.include_abandoned = bool(self.include_abandoned)
        self.include_commits = bool(self.include_commits)

    def selection_key(self) -> str:
        """Which PRs this source covers."""
        created = self.created_after.isoformat() if self.created_after else None
        return json.dumps([self.repositories, self.include_abandoned, created])

    def render_key(self) -> str:
        """Options that change a PR's text without changing its fingerprint. When
        this changes, every PR is rendered again once."""
        return json.dumps([self.include_commits])

    def wants_repo(self, repo: dict) -> bool:
        return not self.repositories or repo.get("name", "").lower() in self.repositories

    def warm(self, pr: dict, now: datetime) -> bool:
        """Whether to read the full PR and threads this run. Activity mostly lands on
        open PRs and ones that closed recently."""
        if self.recheck_closed_days is None or pr.get("status") == "active":
            return True
        closed = pr.get("closedDate")
        if not closed:
            return True
        return now - _parse_time(closed) <= timedelta(days=self.recheck_closed_days)


def sync_pull_requests(
    service: Any,
    scope: _Scope,
    state: dict,
    stats: dict[str, int] | None = None,
    *,
    now: datetime | None = None,
    max_requests: int = DEFAULT_MAX_REQUESTS,
    max_wait_seconds: float = DEFAULT_MAX_WAIT_SECONDS,
    check_active: Callable[[], None] | None = None,
    allow_scope_change: bool = True,
) -> Iterator[dict]:
    """Rows for new or changed PRs, then tombstones.

    ``state["fingerprints"]`` maps each PR key to "<list fp>:<detail fp>". The
    detail half is only compared on runs that fetched it, so an old PR going quiet
    doesn't look like a change. If the run is cut short, the fingerprints of the
    PRs already synced are kept and nothing is deleted, because the listing is
    incomplete; the next run skips what's done and carries on.
    """
    now = now or datetime.now(timezone.utc)
    stats = stats if stats is not None else {}
    for key in ("scanned", "skipped", "deleted", "failed"):
        stats.setdefault(key, 0)
    calls = _Calls(service, max_requests, max_wait_seconds, check_active)
    org_url = service.organization_url

    known: dict[str, str] = dict(state.get("fingerprints", {}))
    stored = (state.get("selection"), state.get("render"))
    current = (scope.selection_key(), scope.render_key())
    if not allow_scope_change and stored[0] is not None and stored != current:
        # With the default resource_name, a second source on the same project would
        # land here on every run and tombstone the other one's pull requests.
        raise ValueError(
            "This source's state was written with different options (repositories, "
            "include_abandoned, created_after or include_commits). To change them, pass "
            "resource_name explicitly, the same name as before. For a second source on "
            "this project, pass a new resource_name."
        )
    if state.get("render") not in (None, scope.render_key()):
        logger.info("Azure Repos: rendering options changed, re-rendering every pull request")
        known = dict.fromkeys(known, "")

    def save() -> None:
        state["fingerprints"] = known
        state["selection"] = scope.selection_key()
        state["render"] = scope.render_key()

    seen: set[str] = set()
    try:
        repos = _repos(calls, scope.project)
        if not repos and known:
            # A failed listing is far likelier than a project with no repos left.
            logger.warning("Azure Repos: no repositories listed but %d PRs known", len(known))
            return
        selected = [r for r in repos if scope.wants_repo(r)]
        missing = set(map(str.lower, scope.repositories)) - {r["name"].lower() for r in selected}
        if missing:
            logger.warning("Azure Repos: repositories not found: %s", ", ".join(sorted(missing)))

        for repo in selected:
            prefix = f"{repo['id']}/"
            if repo.get("isDisabled"):
                # Disabled isn't deleted, and its PRs can't be read. Keep what we have.
                seen |= {key for key in known if key.startswith(prefix)}
                continue
            for pr in _pull_requests(calls, scope.project, repo["id"], scope.created_after):
                if pr.get("status") == "abandoned" and not scope.include_abandoned:
                    continue
                pr_id = pr["pullRequestId"]
                key = prefix + str(pr_id)
                seen.add(key)
                stats["scanned"] += 1
                list_fp = _list_fingerprint(repo, pr)
                old_list_fp, _, old_detail_fp = known.get(key, "").partition(":")
                full: dict | None = None
                threads: list[dict] = []
                detail_fp = None
                if key not in known or scope.warm(pr, now):
                    full = _pull_request(calls, scope.project, repo["id"], pr_id)
                    threads = _threads(calls, scope.project, repo["id"], pr_id)
                    detail_fp = _detail_fingerprint(full, threads)
                if old_list_fp == list_fp and detail_fp in (None, old_detail_fp):
                    stats["skipped"] += 1
                    continue
                if full is None:
                    full = _pull_request(calls, scope.project, repo["id"], pr_id)
                    threads = _threads(calls, scope.project, repo["id"], pr_id)
                    detail_fp = _detail_fingerprint(full, threads)
                commits = (
                    _commits(calls, scope.project, repo["id"], pr_id)
                    if scope.include_commits
                    else []
                )
                known[key] = f"{list_fp}:{detail_fp}"
                content = render_pull_request(repo, full, threads, commits)
                yield _row(repo, full, content, org_url, scope.project)
    except _CutShortError as cut:
        save()
        stats["failed"] = max(1, stats["failed"])
        stats[f"failed_{cut.reason}"] = 1
        logger.warning("Azure Repos stopped early (%s); the next run resumes.", cut.reason)
        return

    for key in sorted(set(known) - seen):
        del known[key]
        stats["deleted"] += 1
        yield {"id": key, "_deleted": True}

    save()
    logger.info("Azure Repos: %(scanned)d scanned, %(skipped)d skipped, %(deleted)d deleted", stats)


def _resource_name(org_url: str, project: str) -> str:
    org = org_url.rstrip("/").rsplit("/", 1)[-1]
    return "azure_repos_" + re.sub(r"[^a-z0-9]+", "_", f"{org}_{project}".lower()).strip("_")


def azure_repos_source(
    *,
    project: str,
    pat: str | None = None,
    organization: str | None = None,
    organization_url: str | None = None,
    repositories: Iterable[str] | None = None,
    include_abandoned: bool = True,
    include_commits: bool = True,
    created_after: date | None = None,
    recheck_closed_days: int | None = 30,
    resource_name: str | None = None,
    max_requests: int = DEFAULT_MAX_REQUESTS,
    max_wait_seconds: float = DEFAULT_MAX_WAIT_SECONDS,
    check_active: Callable[[], None] | None = None,
    service: Any = None,
    client: Any = None,
):
    """A dlt resource with one row per pull request. Use write_disposition="merge".

    Args:
        project: Project name or id.
        pat: Personal access token with Code (Read). Needed unless ``service`` is
            given.
        organization: Name from ``https://dev.azure.com/<organization>``.
        organization_url: Full URL instead, for Azure DevOps Server or
            ``*.visualstudio.com``.
        repositories: Repository names to sync. All of the project's by default.
        include_abandoned: Keep abandoned PRs. They often explain why something
            wasn't done.
        include_commits: Add each PR's commit messages.
        created_after: Skip PRs created before this date.
        recheck_closed_days: Read the full PR and threads of PRs closed within this
            many days on every run. ``None`` reads every PR's, two requests per PR.
        resource_name: Name of this source's table and sync state. Defaults to
            ``azure_repos_<org>_<project>``. With the default, a run refuses state
            written with different options, so two sources can't overwrite each
            other; pass the name explicitly to change the options, or a new name
            for a second source.
        max_requests: Request budget of one run, at least 1. A bigger project
            finishes over several runs.
        max_wait_seconds: How long one run may spend sleeping on Retry-After and
            backoff before it stops cleanly.
        check_active: Optional host authorization checkpoint, called before each
            request.
        service: Pre-built client, mainly for tests. ``client`` is an alias.
    """
    if getattr(dlt_utils, "DOCUMENT_SYNC_VERSION", 0) < 1:
        raise RuntimeError(
            "Azure Repos sync requires cognee>=1.6.3 (table-scoped document cleanup)."
        )
    if not isinstance(max_requests, int) or max_requests < 1:
        raise ValueError("max_requests must be a whole number of at least 1")
    if not max_wait_seconds or max_wait_seconds <= 0:
        raise ValueError("max_wait_seconds must be more than 0")
    service = service or client
    if service is None:
        url = organization_url or (organization and f"https://dev.azure.com/{organization}")
        if not url:
            raise ValueError("Pass organization= (or organization_url= for Azure DevOps Server).")
        if not pat:
            raise ValueError("Pass pat=, a personal access token with Code (Read).")
        service = AzureDevOpsClient(url, pat)

    scope = _Scope(
        project,
        list(repositories or []),
        include_abandoned,
        include_commits,
        created_after,
        recheck_closed_days,
    )
    # Named per org and project by default, and used as the pipeline scope, so two
    # projects (or one project in two datasets) never share fingerprints or a cleanup table.
    name = resource_name or _resource_name(service.organization_url, project)
    stats: dict[str, int] = {}

    @dlt.resource(
        name=name,
        primary_key="id",
        write_disposition="merge",
        columns={"_deleted": {"data_type": "bool", "hard_delete": True}},
    )
    def azure_repos_pull_requests():
        stats.clear()
        rows = sync_pull_requests(
            service,
            scope,
            dlt.current.resource_state(),
            stats,
            max_requests=max_requests,
            max_wait_seconds=max_wait_seconds,
            check_active=check_active,
            allow_scope_change=resource_name is not None,
        )
        yield from dlt_utils.guarded_rows(rows, check_active)

    resource = azure_repos_pull_requests()
    setattr(resource, DOCUMENT_SOURCE_ATTR, SOURCE_TAG)  # route through cognify
    setattr(resource, PIPELINE_SCOPE_ATTR, name)
    resource.cognee_sync_stats = stats  # counts only, never titles or content
    return resource
