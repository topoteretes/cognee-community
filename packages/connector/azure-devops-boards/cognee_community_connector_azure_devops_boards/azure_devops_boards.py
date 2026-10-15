"""Azure DevOps Boards connector: syncs a project's work items into cognee.

One document per work item, on the document path so cognify extracts from it.

* The cursor is the reporting revisions feed's continuationToken. WIQL on
  ChangedDate would hit its 20,000 result cap (VS402337) on a real org.
* Items destroyed from the recycle bin never reach the feed, so each run also
  diffs the ids that still exist against the ones already sent.
* Callers must pass write_disposition="merge". cognee hands it to
  pipeline.run, which overrides the resource hint, and its default is
  "replace", which would forget every work item that didn't change.
* Editing or deleting a comment may not write a revision; it's picked up the
  next time the work item changes.
"""

from __future__ import annotations

import html
import os
import re
import time
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import quote, unquote

import dlt
import requests
from cognee.shared.logging_utils import get_logger
from cognee.tasks.ingestion.dlt_utils import DOCUMENT_SOURCE_ATTR, PIPELINE_SCOPE_ATTR

logger = get_logger("azure_devops_boards_connector")

SOURCE_TAG = "azure_devops_boards"
API_VERSION = "7.1"
COMMENTS_API_VERSION = "7.1-preview.4"

BATCH_SIZE = 200
SWEEP_PAGE_SIZE = 19_999
_MAX_RETRIES = 5
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


class AzureDevOpsClient:
    """Read-only client for one org. The Azure Repos connector reuses it, since
    Azure DevOps rate limits per identity and both share one token."""

    def __init__(self, organization_url: str, pat: str, session: Any = None):
        self.organization_url = organization_url.rstrip("/")
        self.session = session or requests.Session()
        self.session.auth = ("", pat)  # PAT is the password, username is ignored
        self.session.headers.update({"Accept": "application/json"})
        self._wait_until = 0.0

    def get(self, path: str, params: dict | None = None) -> dict:
        return self._request("GET", path, params=params)

    def post(self, path: str, body: dict, params: dict | None = None) -> dict:
        return self._request("POST", path, params=params, json=body)

    def _request(self, method: str, path: str, **kwargs) -> dict:
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
                return resp.json()
            logger.warning("Azure DevOps: HTTP %s on %s, retrying in %.1fs", status, path, wait)
        raise AssertionError("unreachable")


def _retry_after(headers: Any) -> float | None:
    try:
        return max(0.0, float(headers["Retry-After"]))
    except (KeyError, TypeError, ValueError):
        return None


def _changed_ids(
    client: AzureDevOpsClient, project: str, token: str | None
) -> tuple[list[int], str | None]:
    """Ids changed since ``token`` and the token to resume from.

    Type/area filters are applied later: a Bug that becomes a Task still has to
    show up here so it can be dropped.
    """
    path = f"{quote(project)}/_apis/wit/reporting/workitemrevisions"
    params = {
        "api-version": API_VERSION,
        "includeLatestOnly": "true",
        "includeDeleted": "true",
        "fields": "System.Id",
    }
    ids: dict[int, None] = {}
    while True:
        page = client.get(path, {**params, "continuationToken": token} if token else params)
        ids.update(dict.fromkeys(int(rev["id"]) for rev in page.get("values") or []))
        next_token = page.get("continuationToken") or token
        if page.get("isLastBatch", True) or next_token == token:
            return list(ids), next_token
        token = next_token


def _fetch_items(client: AzureDevOpsClient, project: str, ids: list[int]) -> dict[int, dict]:
    """Up to BATCH_SIZE ids. Deleted or hidden ones come back null under
    errorPolicy=Omit and are left out."""
    path = f"{quote(project)}/_apis/wit/workitemsbatch"
    body = {"ids": ids, "$expand": "All", "errorPolicy": "Omit"}
    page = client.post(path, body, {"api-version": API_VERSION})
    return {int(it["id"]): it for it in page.get("value") or [] if it}


def _comments(client: AzureDevOpsClient, project: str, wid: int) -> list[dict]:
    path = f"{quote(project)}/_apis/wit/workItems/{wid}/comments"
    params: dict[str, Any] = {"api-version": COMMENTS_API_VERSION, "$top": 200, "order": "asc"}
    comments: list[dict] = []
    while True:
        page = client.get(path, params)
        comments += page.get("comments") or []
        if not page.get("continuationToken"):
            return comments
        params = {**params, "continuationToken": page["continuationToken"]}


def _existing_ids(client: AzureDevOpsClient, project: str) -> set[int]:
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
        refs = client.post(path, {"query": query}, params).get("workItems") or []
        page = [int(ref["id"]) for ref in refs]
        ids.update(page)
        if len(page) < SWEEP_PAGE_SIZE:
            return ids
        last = max(page)


def _board_labels(client: AzureDevOpsClient, project: str) -> dict[str, str]:
    """Kanban column field name -> "team / board". Needs Project and Team (Read)."""
    version = {"api-version": API_VERSION}
    labels: dict[str, str] = {}
    try:
        teams = client.get(f"_apis/projects/{quote(project)}/teams", version).get("value") or []
        for team in teams:
            boards = f"{quote(project)}/{quote(team['name'])}/_apis/work/boards"
            for board in client.get(boards, version).get("value") or []:
                fields = client.get(f"{boards}/{board['id']}", version).get("fields") or {}
                if column := (fields.get("columnField") or {}).get("referenceName"):
                    labels[column] = f"{team['name']} / {board['name']}"
    except Exception as exc:
        logger.warning(
            "Azure DevOps: can't read team boards (%s), board columns stay unlabelled. "
            "Add the Project and Team (Read) scope to the token for labels.",
            exc,
        )
        return {}
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


def _comment_line(comment: dict) -> str | None:
    text = _clean_html(comment.get("text"))
    if comment.get("isDeleted") or not text:
        return None
    author = _person(comment.get("createdBy")) or "Someone"
    date = (comment.get("createdDate") or "")[:10]
    return f"- {author} on {date}: {text}" if date else f"- {author}: {text}"


def render_work_item(item: dict, comments: list[dict], labels: dict[str, str] | None = None) -> str:
    """Rev and ChangedDate are left out so the content hash only moves on real edits."""
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
    add("Tags", f.get("System.Tags"))
    for label, targets in _links(item.get("relations") or []).items():
        add(label, ", ".join(targets))

    for name, heading in _SECTIONS:
        if text := _clean_html(f.get(name)):
            lines.append(f"\n{heading}:\n{text}")

    if thread := [line for line in map(_comment_line, comments) if line]:
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

    def contains(self, item: dict) -> bool:
        f = item.get("fields") or {}
        if self.work_item_types and f.get("System.WorkItemType") not in self.work_item_types:
            return False
        area = (f.get("System.AreaPath") or "").lower()
        # "Shop\Pay" must not match "Shop\Payments"
        return not self.area_paths or any(
            area == p.lower() or area.startswith(p.lower() + "\\") for p in self.area_paths
        )


def sync_work_items(client: AzureDevOpsClient, scope: _Scope, state: dict) -> Iterator[dict]:
    """Rows for changed work items, then tombstones. ``state`` is only written at
    the end, so a failed run retries from the same cursor."""
    known = {int(i) for i in state.get("known_ids", [])}
    changed, token = _changed_ids(client, scope.project, state.get("continuation_token"))

    sent = removed = 0
    labels: dict[str, str] | None = None  # fetched once, only if an item is on a board
    # One batch at a time, so a first sync of a big project never holds it all in memory.
    for i in range(0, len(changed), BATCH_SIZE):
        batch = changed[i : i + BATCH_SIZE]
        items = _fetch_items(client, scope.project, batch)
        for wid in batch:
            item = items.get(wid)
            if item is None or not scope.contains(item):
                if wid in known:
                    known.discard(wid)
                    removed += 1
                    yield _tombstone(wid)
                continue
            if labels is None and _on_board(item):
                labels = _board_labels(client, scope.project)
            # Skip the extra request when the item says it has no comments.
            has_comments = (item.get("fields") or {}).get("System.CommentCount", 1)
            comments = (
                _comments(client, scope.project, wid)
                if scope.include_comments and has_comments
                else []
            )
            known.add(wid)
            sent += 1
            content = render_work_item(item, comments, labels)
            yield _row(item, content, client.organization_url, scope.project)

    existing = _existing_ids(client, scope.project)
    if known and not existing:
        # A failed listing is far likelier than an emptied project; don't forget everything.
        logger.warning("Azure DevOps: empty id sweep with %d known, skipping deletes", len(known))
    else:
        for wid in sorted(known - existing):
            removed += 1
            yield _tombstone(wid)
        known &= existing

    state["continuation_token"] = token
    state["known_ids"] = sorted(known)
    logger.info("Azure DevOps Boards: %d changed, %d removed", sent, removed)


def _resource_name(org_url: str, project: str) -> str:
    org = org_url.rstrip("/").rsplit("/", 1)[-1]
    return "azure_devops_" + re.sub(r"[^a-z0-9]+", "_", f"{org}_{project}".lower()).strip("_")


def azure_devops_boards_source(
    *,
    organization: str | None = None,
    project: str,
    pat: str | None = None,
    organization_url: str | None = None,
    area_paths: Iterable[str] | None = None,
    work_item_types: Iterable[str] | None = None,
    include_comments: bool = True,
    client: AzureDevOpsClient | None = None,
):
    """A dlt resource with one row per work item. Use write_disposition="merge".

    Args:
        organization: Name from ``https://dev.azure.com/<organization>``.
        project: Project name or id.
        pat: Token with Work Items (Read). Defaults to ``AZURE_DEVOPS_PAT``.
        organization_url: Full URL instead, for Azure DevOps Server or
            ``*.visualstudio.com``.
        area_paths: Only these area paths and everything under them.
        work_item_types: Only these types, e.g. ``["Bug", "User Story"]``.
        include_comments: Add each work item's comments to its text.
        client: Ready-made client, for tests.
    """
    if client is None:
        url = organization_url or (organization and f"https://dev.azure.com/{organization}")
        token = pat or os.environ.get("AZURE_DEVOPS_PAT")
        if not url or not token:
            raise ValueError(
                "Need organization= (or organization_url=) and pat= (or AZURE_DEVOPS_PAT)."
            )
        client = AzureDevOpsClient(url, token)

    scope = _Scope(project, list(area_paths or []), list(work_item_types or []), include_comments)

    # Named per org and project, and used as the pipeline scope, so two projects
    # (or one project in two datasets) never share a cursor or a cleanup table.
    name = _resource_name(client.organization_url, project)

    @dlt.resource(
        name=name,
        primary_key="id",
        write_disposition="merge",
        columns={"_deleted": {"data_type": "bool", "hard_delete": True}},
    )
    def azure_devops_work_items():
        yield from sync_work_items(client, scope, dlt.current.resource_state())

    resource = azure_devops_work_items()
    setattr(resource, DOCUMENT_SOURCE_ATTR, SOURCE_TAG)  # route through cognify
    setattr(resource, PIPELINE_SCOPE_ATTR, name)
    return resource
