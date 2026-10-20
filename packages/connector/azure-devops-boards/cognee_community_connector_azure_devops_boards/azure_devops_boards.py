"""Azure DevOps Boards connector: syncs a project's work items into cognee.

One document per work item, on the document path so cognify extracts from it.

* The cursor is the reporting revisions feed's continuationToken. WIQL on
  ChangedDate would hit its 20,000 result cap (VS402337) on a real org.
* A work item leaves memory only when the feed marks it deleted, when it moves
  out of the selected scope, or when the id sweep no longer finds it (destroyed
  from the recycle bin). An item the batch call merely leaves out keeps its
  document.
* Changing ``area_paths``, ``work_item_types`` or ``include_comments`` re-reads
  the whole project once, so newly selected items come in and deselected ones
  are forgotten.
* Each run has a request budget. When it runs out, or Azure DevOps keeps
  answering 429, the run stops cleanly and the next one carries on from there.
* Callers must pass write_disposition="merge". cognee hands it to
  pipeline.run, which overrides the resource hint, and its default is
  "replace", which would forget every work item that didn't change.
* Editing or deleting a comment may not write a revision; it's picked up the
  next time the work item changes.
"""

from __future__ import annotations

import html
import json
import re
import time
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import quote, unquote

import dlt
import requests
from cognee.shared.logging_utils import get_logger
from cognee.tasks.ingestion import dlt_utils
from cognee.tasks.ingestion.dlt_utils import DOCUMENT_SOURCE_ATTR, PIPELINE_SCOPE_ATTR

logger = get_logger("azure_devops_boards_connector")

SOURCE_TAG = "azure_devops_boards"
API_VERSION = "7.1"
# the work item comments API never left preview, even on 7.1
COMMENTS_API_VERSION = "7.1-preview.4"

# workitemsbatch rejects more than 200 ids per call
BATCH_SIZE = 200
# WIQL fails with VS402337 above 20,000 results, so the sweep asks for one less and pages by id
SWEEP_PAGE_SIZE = 19_999
# requests per run; a bigger project finishes its first sync over several runs
DEFAULT_MAX_REQUESTS = 5_000
# attempts per request on 429, 5xx or network errors
_MAX_ATTEMPTS = 5
# our own ceiling on a single Retry-After wait; Azure DevOps doesn't document one
_MAX_RETRY_AFTER = 60.0
_TIMEOUT = 60

_SECTIONS = (
    ("System.Description", "Description"),
    ("Microsoft.VSTS.TCM.ReproSteps", "Repro steps"),
    ("Microsoft.VSTS.Common.AcceptanceCriteria", "Acceptance criteria"),
)

_LINK_LABELS = {
    "System.LinkTypes.Hierarchy-Reverse": "Parent",
    "System.LinkTypes.Hierarchy-Forward": "Children",
    "System.LinkTypes.Related": "Related",
    "System.LinkTypes.Dependency-Forward": "Successors",
    "System.LinkTypes.Dependency-Reverse": "Predecessors",
    "System.LinkTypes.Duplicate-Forward": "Duplicates",
    "System.LinkTypes.Duplicate-Reverse": "Duplicate of",
}

_KANBAN_RE = re.compile(r"^WEF_[0-9A-Fa-f]+_Kanban\.Column$")
_PR_RE = re.compile(r"^vstfs:///Git/PullRequestId/(.+)$", re.IGNORECASE)
_TAG_RE = re.compile(r"<[^>]+>")
_BREAK_RE = re.compile(r"<\s*(br|/p|/div|/li|/h[1-6])\s*/?>", re.IGNORECASE)
_SPACES_RE = re.compile(r"[ \t\r\f\v\u00a0]+")
_BLANKS_RE = re.compile(r"\n\s*\n+")


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


_ERRORS = {
    401: (AzureDevOpsAuthError, "check the personal access token"),
    403: (AzureDevOpsAccessError, "the token is missing a scope this call needs"),
    429: (AzureDevOpsRateLimitedError, "rate limited"),
}


class AzureDevOpsClient:
    """Read-only client for one org. The Azure Repos connector uses the same one,
    since Azure DevOps rate limits per identity and both share one token."""

    def __init__(
        self,
        organization_url: str,
        pat: str,
        session: Any = None,
        sleep: Callable[[float], None] | None = None,
    ):
        if not pat:
            raise ValueError("A personal access token is required")
        self.organization_url = organization_url.rstrip("/")
        self.session = session or requests.Session()
        self.session.auth = ("", pat)  # PAT is the password, username is ignored
        self.session.headers.update({"Accept": "application/json"})
        self._sleep = sleep or time.sleep
        self._wait_until = 0.0

    def __repr__(self) -> str:
        return f"AzureDevOpsClient({self.organization_url!r}, pat=<redacted>)"

    def get(self, path: str, params: dict | None = None) -> dict:
        return self._request("GET", path, params=params)

    def post(self, path: str, body: dict, params: dict | None = None) -> dict:
        return self._request("POST", path, params=params, json=body)

    def _request(self, method: str, path: str, **kwargs: Any) -> dict:
        url = path if path.startswith("http") else f"{self.organization_url}/{path.lstrip('/')}"
        attempt = 0
        while True:
            attempt += 1
            if (delay := self._wait_until - time.monotonic()) > 0:
                self._sleep(delay)
            try:
                resp = self.session.request(method, url, timeout=_TIMEOUT, **kwargs)
            except requests.RequestException as exc:
                if attempt >= _MAX_ATTEMPTS:
                    raise AzureDevOpsError("Azure DevOps could not be reached", url=url) from exc
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
            if status >= 400:
                error, hint = _ERRORS.get(status, (AzureDevOpsError, "request failed"))
                raise error(f"Azure DevOps returned HTTP {status}: {hint}", status=status, url=url)
            return resp.json()


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
    """Every request of one run goes through here: host check, then the budget."""

    def __init__(self, service: Any, max_requests: int, check_active: Callable[[], None] | None):
        self.service = service
        self.max_requests = max_requests
        self.check_active = check_active
        self.made = 0

    def get(self, path: str, params: dict | None = None) -> dict:
        return self._call(self.service.get, path, params)

    def post(self, path: str, body: dict, params: dict | None = None) -> dict:
        return self._call(self.service.post, path, body, params)

    def _call(self, method: Callable[..., dict], *args: Any) -> dict:
        if self.check_active is not None:
            self.check_active()
        if self.made >= self.max_requests:
            raise _CutShortError("budget")
        self.made += 1
        try:
            return method(*args)
        except AzureDevOpsRateLimitedError:
            raise _CutShortError("rate_limit") from None


def _feed_page(calls: _Calls, project: str, token: str | None) -> dict:
    path = f"{quote(project)}/_apis/wit/reporting/workitemrevisions"
    params = {
        "api-version": API_VERSION,
        "includeLatestOnly": "true",
        "includeDeleted": "true",
        "fields": "System.Id,System.IsDeleted",
    }
    return calls.get(path, {**params, "continuationToken": token} if token else params)


def _is_deleted(revision: dict) -> bool:
    fields = revision.get("fields") or {}
    return bool(revision.get("isDeleted") or fields.get("System.IsDeleted"))


def _fetch_items(calls: _Calls, project: str, ids: list[int]) -> dict[int, dict]:
    """Up to BATCH_SIZE ids. Under errorPolicy=Omit an id the token can't read comes
    back null, for any reason, so a missing id is not proof of deletion."""
    path = f"{quote(project)}/_apis/wit/workitemsbatch"
    body = {"ids": ids, "$expand": "All", "errorPolicy": "Omit"}
    page = calls.post(path, body, {"api-version": API_VERSION})
    return {int(it["id"]): it for it in page.get("value") or [] if it}


def _comments(calls: _Calls, project: str, wid: int) -> list[dict]:
    path = f"{quote(project)}/_apis/wit/workItems/{wid}/comments"
    params: dict[str, Any] = {"api-version": COMMENTS_API_VERSION, "$top": 200, "order": "asc"}
    comments: list[dict] = []
    while True:
        page = calls.get(path, params)
        comments += page.get("comments") or []
        if not page.get("continuationToken"):
            return comments
        params = {**params, "continuationToken": page["continuationToken"]}


def _existing_ids(calls: _Calls, project: str) -> set[int]:
    """All ids still in the project (WIQL skips the recycle bin), paged by id."""
    path = f"{quote(project)}/_apis/wit/wiql"
    params = {"api-version": API_VERSION, "$top": SWEEP_PAGE_SIZE}
    ids: set[int] = set()
    last = 0
    while True:
        query = (
            "SELECT [System.Id] FROM WorkItems "
            f"WHERE [System.TeamProject] = @project AND [System.Id] > {last} "
            "ORDER BY [System.Id] ASC"
        )
        refs = calls.post(path, {"query": query}, params).get("workItems") or []
        page = [int(ref["id"]) for ref in refs]
        ids.update(page)
        if len(page) < SWEEP_PAGE_SIZE:
            return ids
        last = max(page)


def _board_labels(calls: _Calls, project: str, state: dict) -> dict[str, str]:
    """Kanban column field name -> "team / board", cached in state.

    Needs Project and Team (Read). Without it the last labels we had are kept, or
    none if there never were any, so the text doesn't flip between runs. Any
    other error fails the run rather than rendering different text.
    """
    version = {"api-version": API_VERSION}
    labels: dict[str, str] = {}
    try:
        teams = calls.get(f"_apis/projects/{quote(project)}/teams", version).get("value") or []
        for team in teams:
            boards = f"{quote(project)}/{quote(team['name'])}/_apis/work/boards"
            for board in calls.get(boards, version).get("value") or []:
                fields = calls.get(f"{boards}/{board['id']}", version).get("fields") or {}
                if column := (fields.get("columnField") or {}).get("referenceName"):
                    labels[column] = f"{team['name']} / {board['name']}"
    except (AzureDevOpsAuthError, AzureDevOpsAccessError) as exc:
        logger.warning(
            "Azure DevOps: can't read team boards (%s). Add the Project and Team (Read) "
            "scope to label board columns by team.",
            exc,
        )
        return dict(state.get("board_labels") or {})
    state["board_labels"] = labels
    return labels


def _clean_html(raw: Any) -> str:
    if not raw:
        return ""
    text = html.unescape(_TAG_RE.sub(" ", _BREAK_RE.sub("\n", str(raw))))
    lines = (line.strip() for line in _SPACES_RE.sub(" ", text).split("\n"))
    return _BLANKS_RE.sub("\n\n", "\n".join(lines)).strip()


def _person(value: Any) -> str:
    """Display name only, emails stay out of memory."""
    if isinstance(value, dict):
        return value.get("displayName") or ""
    if isinstance(value, str):  # older payloads: "Name <email>"
        return value.split("<")[0].strip()
    return ""


def _links(relations: list[dict]) -> dict[str, list[str]]:
    """Written as "work item #123" / "pull request !45", same as the Repos
    connector, so both end up on the same graph node."""
    groups: dict[str, list[str]] = {}
    for rel in relations:
        kind, url = rel.get("rel") or "", rel.get("url") or ""
        if kind == "ArtifactLink":
            if pr := _PR_RE.match(url):
                pr_id = unquote(pr.group(1)).rsplit("/", 1)[-1]  # project/repo/id, url-encoded
                groups.setdefault("Pull requests", []).append(f"pull request !{pr_id}")
            continue
        target = url.rstrip("/").rsplit("/", 1)[-1]
        if kind in _LINK_LABELS and target.isdigit():
            groups.setdefault(_LINK_LABELS[kind], []).append(f"work item #{target}")
    return {label: sorted(v) for label, v in sorted(groups.items())}


def _on_board(item: dict) -> bool:
    return any(map(_KANBAN_RE.match, item.get("fields") or {}))


def _board_columns(fields: dict, labels: dict[str, str]) -> list[str]:
    columns = [
        f"{fields[name]} ({labels[name]})" if name in labels else str(fields[name])
        for name in sorted(fields)
        if _KANBAN_RE.match(name) and fields[name]
    ]
    if not columns and fields.get("System.BoardColumn"):
        columns.append(str(fields["System.BoardColumn"]))
    return columns


def _tags(value: Any) -> str:
    return "; ".join(sorted(t.strip() for t in str(value or "").split(";") if t.strip()))


def _comment_line(comment: dict) -> str | None:
    text = _clean_html(comment.get("text"))
    if comment.get("isDeleted") or not text:
        return None
    author = _person(comment.get("createdBy")) or "Someone"
    date = (comment.get("createdDate") or "")[:10]
    return f"- {author} on {date}: {text}" if date else f"- {author}: {text}"


def render_work_item(item: dict, comments: list[dict], labels: dict[str, str] | None = None) -> str:
    """Rev and ChangedDate are left out, and every collection is sorted, so the
    content hash only moves on real edits."""
    f = item.get("fields") or {}
    lines: list[str] = []

    def add(label: str, value: Any) -> None:
        if value:
            lines.append(f"{label}: {value}")

    add("Type", f.get("System.WorkItemType"))
    add("State", f.get("System.State"))
    add("Reason", f.get("System.Reason"))
    add("Board column", "; ".join(_board_columns(f, labels or {})))
    add("Assigned to", _person(f.get("System.AssignedTo")))
    add("Created by", _person(f.get("System.CreatedBy")))
    add("Area", f.get("System.AreaPath"))
    add("Iteration", f.get("System.IterationPath"))
    add("Priority", f.get("Microsoft.VSTS.Common.Priority"))
    add("Severity", f.get("Microsoft.VSTS.Common.Severity"))
    add("Story points", f.get("Microsoft.VSTS.Scheduling.StoryPoints"))
    add("Tags", _tags(f.get("System.Tags")))
    for label, targets in _links(item.get("relations") or []).items():
        add(label, ", ".join(targets))

    for name, heading in _SECTIONS:
        if text := _clean_html(f.get(name)):
            lines.append(f"\n{heading}:\n{text}")

    ordered = sorted(comments, key=lambda c: (c.get("createdDate") or "", c.get("id") or 0))
    if thread := [line for line in map(_comment_line, ordered) if line]:
        lines.append("\nComments:\n" + "\n".join(thread))

    return "\n".join(lines).strip()


def _row(item: dict, content: str, org_url: str, project: str) -> dict[str, Any]:
    f = item.get("fields") or {}
    wid = int(item["id"])
    title = f"{f.get('System.WorkItemType') or 'Work item'} #{wid}"
    if f.get("System.Title"):
        title += f": {f['System.Title']}"
    return {
        "id": str(wid),
        "title": title,
        "content": content,
        "url": f"{org_url}/{quote(project)}/_workitems/edit/{wid}",
        "_deleted": False,
    }


def _tombstone(wid: int) -> dict[str, Any]:
    return {"id": str(wid), "_deleted": True}


@dataclass
class _Scope:
    project: str
    area_paths: list[str] = field(default_factory=list)
    work_item_types: list[str] = field(default_factory=list)
    include_comments: bool = True

    def key(self) -> str:
        """Changes when the selection changes, which forces one full re-read."""
        return json.dumps(
            [
                sorted(p.lower() for p in self.area_paths),
                sorted(self.work_item_types),
                self.include_comments,
            ]
        )

    def contains(self, item: dict) -> bool:
        f = item.get("fields") or {}
        if self.work_item_types and f.get("System.WorkItemType") not in self.work_item_types:
            return False
        area = (f.get("System.AreaPath") or "").lower()
        # "Shop\Pay" must not match "Shop\Payments"
        return not self.area_paths or any(
            area == p.lower() or area.startswith(p.lower() + "\\") for p in self.area_paths
        )


def sync_work_items(
    service: Any,
    scope: _Scope,
    state: dict,
    stats: dict[str, int] | None = None,
    *,
    max_requests: int = DEFAULT_MAX_REQUESTS,
    check_active: Callable[[], None] | None = None,
) -> Iterator[dict]:
    """Rows for changed work items, then tombstones.

    ``state`` holds the feed token, the ids already sent, ids read from the feed
    but not handled yet, the scope they were read under, and the board labels.
    It's written when the run ends or is cut short, never half way through a
    step, so a failure retries from the last good point.
    """
    stats = stats if stats is not None else {}
    for key in ("scanned", "skipped", "deleted", "failed"):
        stats.setdefault(key, 0)
    calls = _Calls(service, max_requests, check_active)
    project = scope.project

    known = {int(i) for i in state.get("known_ids", [])}
    token = state.get("continuation_token")
    if state.get("scope") not in (None, scope.key()):
        logger.info("Azure DevOps Boards: selection changed, re-reading the whole project")
        token = None
    work: dict[int, None] = dict.fromkeys(int(i) for i in state.get("pending_ids", []))
    deleted = {int(i) for i in state.get("pending_deleted", [])}

    def save(next_token: str | None, pending: list[int]) -> None:
        state["continuation_token"] = next_token
        state["pending_ids"] = pending
        state["pending_deleted"] = sorted(deleted.intersection(pending))
        state["known_ids"] = sorted(known)
        state["scope"] = scope.key()

    def cut_short(reason: str) -> None:
        stats["failed"] = max(1, stats["failed"])
        stats[f"failed_{reason}"] = 1
        logger.warning("Azure DevOps Boards stopped early (%s); the next run resumes.", reason)

    # 1. Read the feed. A stuck token raises instead of silently dropping changes.
    try:
        while True:
            page = _feed_page(calls, project, token)
            for revision in page.get("values") or []:
                wid = int(revision["id"])
                work[wid] = None
                (deleted.add if _is_deleted(revision) else deleted.discard)(wid)
            next_token = page.get("continuationToken")
            if page.get("isLastBatch", True):
                token = next_token or token
                break
            if not next_token or next_token == token:
                raise AzureDevOpsError(
                    "The revisions feed returned the same continuationToken without "
                    "finishing; stopping so no changes are skipped"
                )
            token = next_token
    except _CutShortError as cut:
        save(token, list(work))
        cut_short(cut.reason)
        return

    # 2. Fetch and render, one batch at a time.
    ids = list(work)
    pos = 0
    labels: dict[str, str] | None = None
    try:
        for start in range(0, len(ids), BATCH_SIZE):
            batch = ids[start : start + BATCH_SIZE]
            items = _fetch_items(calls, project, batch)
            for wid in batch:
                stats["scanned"] += 1
                item = items.get(wid)
                gone = wid in deleted if item is None else not scope.contains(item)
                if item is None and not gone:
                    stats["skipped"] += 1  # left out but not deleted: the sweep decides
                elif gone:
                    if wid in known:
                        known.discard(wid)
                        stats["deleted"] += 1
                        yield _tombstone(wid)
                    else:
                        stats["skipped"] += 1
                else:
                    if labels is None and _on_board(item):
                        labels = _board_labels(calls, project, state)
                    # skip the extra request when the item says it has no comments
                    count = (item.get("fields") or {}).get("System.CommentCount", 1)
                    wants = scope.include_comments and count
                    comments = _comments(calls, project, wid) if wants else []
                    known.add(wid)
                    yield _row(
                        item, render_work_item(item, comments, labels), _org(service), project
                    )
                pos += 1
    except _CutShortError as cut:
        save(token, ids[pos:])
        cut_short(cut.reason)
        return

    # 3. Destroyed items never reach the feed again, so diff against what exists.
    try:
        existing = _existing_ids(calls, project)
    except _CutShortError as cut:
        save(token, [])
        cut_short(cut.reason)
        return
    if known and not existing:
        # A failed listing is far likelier than an emptied project; don't forget everything.
        logger.warning("Azure DevOps: empty id sweep with %d known, skipping deletes", len(known))
    else:
        for wid in sorted(known - existing):
            stats["deleted"] += 1
            yield _tombstone(wid)
        known &= existing

    save(token, [])
    logger.info(
        "Azure DevOps Boards: %(scanned)d scanned, %(skipped)d skipped, %(deleted)d deleted",
        stats,
    )


def _org(service: Any) -> str:
    return service.organization_url


def _resource_name(org_url: str, project: str) -> str:
    org = org_url.rstrip("/").rsplit("/", 1)[-1]
    return "azure_devops_" + re.sub(r"[^a-z0-9]+", "_", f"{org}_{project}".lower()).strip("_")


def azure_devops_boards_source(
    *,
    project: str,
    pat: str | None = None,
    organization: str | None = None,
    organization_url: str | None = None,
    area_paths: Iterable[str] | None = None,
    work_item_types: Iterable[str] | None = None,
    include_comments: bool = True,
    resource_name: str | None = None,
    max_requests: int = DEFAULT_MAX_REQUESTS,
    check_active: Callable[[], None] | None = None,
    service: Any = None,
    client: Any = None,
):
    """A dlt resource with one row per work item. Use write_disposition="merge".

    Args:
        project: Project name or id.
        pat: Personal access token with Work Items (Read). Needed unless
            ``service`` is given.
        organization: Name from ``https://dev.azure.com/<organization>``.
        organization_url: Full URL instead, for Azure DevOps Server or
            ``*.visualstudio.com``.
        area_paths: Only these area paths and everything under them.
        work_item_types: Only these types, e.g. ``["Bug", "User Story"]``.
        include_comments: Add each work item's comments to its text.
        resource_name: Name of this source's table and sync state. Defaults to
            ``azure_devops_<org>_<project>``. Give each source its own name when
            one project is synced twice with different selections.
        max_requests: Request budget of one run. A bigger project finishes over
            several runs.
        check_active: Optional host authorization checkpoint, called before each
            request.
        service: Pre-built client, mainly for tests. ``client`` is an alias.
    """
    if getattr(dlt_utils, "DOCUMENT_SYNC_VERSION", 0) < 1:
        raise RuntimeError(
            "Azure DevOps Boards sync requires cognee>=1.6.3 (table-scoped document cleanup)."
        )
    service = service or client
    if service is None:
        url = organization_url or (organization and f"https://dev.azure.com/{organization}")
        if not url:
            raise ValueError("Pass organization= (or organization_url= for Azure DevOps Server).")
        if not pat:
            raise ValueError("Pass pat=, a personal access token with Work Items (Read).")
        service = AzureDevOpsClient(url, pat)

    scope = _Scope(project, list(area_paths or []), list(work_item_types or []), include_comments)
    # Named per org and project by default, and used as the pipeline scope, so two
    # projects (or one project in two datasets) never share a cursor or a cleanup table.
    name = resource_name or _resource_name(_org(service), project)
    stats: dict[str, int] = {}

    @dlt.resource(
        name=name,
        primary_key="id",
        write_disposition="merge",
        columns={"_deleted": {"data_type": "bool", "hard_delete": True}},
    )
    def azure_devops_work_items():
        stats.clear()
        rows = sync_work_items(
            service,
            scope,
            dlt.current.resource_state(),
            stats,
            max_requests=max_requests,
            check_active=check_active,
        )
        yield from dlt_utils.guarded_rows(rows, check_active)

    resource = azure_devops_work_items()
    setattr(resource, DOCUMENT_SOURCE_ATTR, SOURCE_TAG)  # route through cognify
    setattr(resource, PIPELINE_SCOPE_ATTR, name)
    resource.cognee_sync_stats = stats  # counts only, never titles or content
    return resource
