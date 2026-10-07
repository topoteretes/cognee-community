"""Incremental Asana document source for cognee.

The connector keeps a lightweight inventory of the projects, tasks, and
subtasks in the selected scope. Task bodies are fetched only when Asana's
``modified_since`` query reports a change, a task is new, its ``modified_at``
value changes, or its comment fingerprint changes. The last condition matters
because comment-only activity has not always advanced the task cursor.

Rows use stable, type-prefixed ids and dlt ``merge`` semantics. Objects that
disappear from the current inventory are emitted as hard-delete tombstones;
cognee's normal orphan cleanup then removes them from memory.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from collections import deque
from collections.abc import Iterator, Sequence
from typing import Any

from cognee.shared.logging_utils import get_logger
from cognee.tasks.ingestion.dlt_utils import DOCUMENT_SOURCE_ATTR

logger = get_logger("asana_connector")

ASANA_SOURCE_NAME = "asana"
ASANA_TABLE_NAME = "asana_documents"

_API_BASE_URL = "https://app.asana.com/api/1.0"
_MAX_RETRIES = 5
_REQUEST_TIMEOUT = 30
_ALL_COMPLETED_SINCE = "1970-01-01T00:00:00.000Z"

_PROJECT_FIELDS = ",".join(
    (
        "gid",
        "name",
        "notes",
        "html_notes",
        "archived",
        "color",
        "created_at",
        "modified_at",
        "permalink_url",
        "owner.name",
        "team.name",
        "workspace.name",
    )
)
_TASK_STUB_FIELDS = "gid,modified_at,parent.gid"
_TASK_FIELDS = ",".join(
    (
        "gid",
        "name",
        "notes",
        "html_notes",
        "completed",
        "completed_at",
        "created_at",
        "modified_at",
        "due_at",
        "due_on",
        "start_at",
        "start_on",
        "permalink_url",
        "resource_subtype",
        "assignee.name",
        "parent.gid",
        "parent.name",
        "memberships.project.gid",
        "memberships.project.name",
        "memberships.section.name",
        "tags.name",
        "custom_fields.name",
        "custom_fields.display_value",
    )
)
_STORY_FIELDS = ",".join(
    (
        "gid",
        "resource_subtype",
        "type",
        "text",
        "created_at",
        "created_by.name",
    )
)


class AsanaAPIError(RuntimeError):
    """Raised when Asana returns a malformed successful response."""


class AsanaNotFoundError(AsanaAPIError):
    """Raised when a selected Asana object no longer exists."""


def _make_session(token: str) -> Any:
    """Build an authenticated, read-only Asana HTTP session."""
    try:
        import requests
    except ImportError as exc:  # pragma: no cover - optional dependency guard
        raise ImportError(
            'Install the Asana connector with: pip install "cognee-community-connector-asana"'
        ) from exc

    session = requests.Session()
    session.headers.update(
        {
            "Accept": "application/json",
            "Authorization": f"Bearer {token}",
        }
    )
    return session


def _retry_delay(headers: Any, attempt: int) -> float:
    value = (headers or {}).get("Retry-After") or (headers or {}).get("retry-after")
    try:
        return float(value)
    except (TypeError, ValueError):
        return float(2**attempt)


def _api_get(client: Any, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
    """GET an Asana API path, retrying rate limits and transient server errors."""
    url = f"{_API_BASE_URL}/{path.lstrip('/')}"

    for attempt in range(_MAX_RETRIES):
        response = client.get(url, params=params or {}, timeout=_REQUEST_TIMEOUT)
        status = getattr(response, "status_code", 200)
        if status not in {429, 500, 502, 503, 504}:
            if status == 404:
                raise AsanaNotFoundError(f"Asana object not found for {path!r}.")
            response.raise_for_status()
            payload = response.json()
            if not isinstance(payload, dict) or "data" not in payload:
                raise AsanaAPIError(f"Asana returned an invalid response for {path!r}.")
            return payload

        if attempt == _MAX_RETRIES - 1:
            response.raise_for_status()

        delay = _retry_delay(getattr(response, "headers", None), attempt)
        logger.warning(
            "Asana request was rate-limited or unavailable (HTTP %s); retrying in %.1fs.",
            status,
            delay,
        )
        time.sleep(delay)

    raise AssertionError("unreachable")


def _paginate(
    client: Any,
    path: str,
    params: dict[str, Any] | None = None,
) -> Iterator[dict[str, Any]]:
    """Yield objects from an Asana collection using opaque offset tokens."""
    request_params = dict(params or {})
    request_params.setdefault("limit", 100)
    seen_offsets: set[str] = set()

    while True:
        payload = _api_get(client, path, request_params)
        data = payload.get("data")
        if not isinstance(data, list):
            raise AsanaAPIError(f"Asana returned non-list data for collection {path!r}.")
        yield from data

        next_page = payload.get("next_page")
        if not next_page:
            return
        offset = next_page.get("offset") if isinstance(next_page, dict) else None
        if not offset:
            raise AsanaAPIError(f"Asana pagination for {path!r} omitted the next offset.")
        if offset in seen_offsets:
            raise AsanaAPIError(f"Asana pagination for {path!r} repeated offset {offset!r}.")
        seen_offsets.add(offset)
        request_params["offset"] = offset


def _get_one(client: Any, path: str, fields: str) -> dict[str, Any]:
    data = _api_get(client, path, {"opt_fields": fields}).get("data")
    if not isinstance(data, dict):
        raise AsanaAPIError(f"Asana returned non-object data for {path!r}.")
    return data


def _resolve_project_ids(
    client: Any,
    project_ids: Sequence[str] | None,
    workspace_id: str | None,
) -> list[str]:
    """Resolve an explicit project selection or every project in one workspace."""
    if isinstance(project_ids, str):
        raise TypeError("project_ids must be a sequence of project GIDs, not a string.")
    normalized = list(
        dict.fromkeys(str(item).strip() for item in project_ids or [] if str(item).strip())
    )
    if normalized and workspace_id:
        raise ValueError("Pass project_ids or workspace_id, not both.")
    if normalized:
        return normalized
    if not workspace_id:
        raise ValueError("Select what to ingest with project_ids or workspace_id.")

    projects = _paginate(
        client,
        f"workspaces/{workspace_id}/projects",
        {"archived": False, "opt_fields": "gid"},
    )
    return [str(project["gid"]) for project in projects]


def _project_details(client: Any, project_id: str) -> dict[str, Any]:
    return _get_one(client, f"projects/{project_id}", _PROJECT_FIELDS)


def _project_task_stubs(client: Any, project_id: str) -> Iterator[dict[str, Any]]:
    yield from _paginate(
        client,
        f"projects/{project_id}/tasks",
        {
            "completed_since": _ALL_COMPLETED_SINCE,
            "opt_fields": _TASK_STUB_FIELDS,
        },
    )


def _changed_project_task_ids(
    client: Any,
    project_id: str,
    modified_since: str,
) -> set[str]:
    """Return tasks changed since the stored cursor for one project."""
    if not modified_since:
        return set()
    return {
        str(task["gid"])
        for task in _paginate(
            client,
            "tasks",
            {
                "project": project_id,
                "completed_since": _ALL_COMPLETED_SINCE,
                "modified_since": modified_since,
                "opt_fields": _TASK_STUB_FIELDS,
            },
        )
    }


def _subtask_stubs(client: Any, task_id: str) -> Iterator[dict[str, Any]]:
    yield from _paginate(
        client,
        f"tasks/{task_id}/subtasks",
        {"opt_fields": _TASK_STUB_FIELDS},
    )


def _discover_tasks(client: Any, project_ids: Sequence[str]) -> dict[str, dict[str, Any]]:
    """Build the complete current task/subtask inventory for selected projects."""
    tasks: dict[str, dict[str, Any]] = {}
    queue: deque[str] = deque()

    for project_id in project_ids:
        for task in _project_task_stubs(client, project_id):
            task_id = str(task["gid"])
            if task_id not in tasks:
                tasks[task_id] = task
                queue.append(task_id)

    visited: set[str] = set()
    while queue:
        task_id = queue.popleft()
        if task_id in visited:
            continue
        visited.add(task_id)
        for subtask in _subtask_stubs(client, task_id):
            subtask_id = str(subtask["gid"])
            if subtask_id not in tasks:
                tasks[subtask_id] = subtask
                queue.append(subtask_id)

    return tasks


def _task_comments(client: Any, task_id: str) -> list[dict[str, Any]]:
    """Return text-bearing comment stories for a task."""
    stories = _paginate(
        client,
        f"tasks/{task_id}/stories",
        {"opt_fields": _STORY_FIELDS},
    )
    return [story for story in stories if _is_comment(story) and story.get("text")]


def _is_comment(story: dict[str, Any]) -> bool:
    return story.get("type") == "comment" or story.get("resource_subtype") == "comment_added"


def _fingerprint(value: Any) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(encoded.encode()).hexdigest()


def _story_fingerprint(stories: Sequence[dict[str, Any]]) -> str:
    relevant = [
        {
            "gid": story.get("gid"),
            "text": story.get("text"),
            "created_at": story.get("created_at"),
            "created_by": (story.get("created_by") or {}).get("name"),
        }
        for story in stories
    ]
    return _fingerprint(relevant)


def _task_details(client: Any, task_id: str) -> dict[str, Any]:
    return _get_one(client, f"tasks/{task_id}", _TASK_FIELDS)


def _name(value: Any) -> str:
    return str((value or {}).get("name") or "") if isinstance(value, dict) else ""


def _append_metadata(lines: list[str], label: str, value: Any) -> None:
    if value not in (None, "", [], {}):
        lines.append(f"- {label}: {value}")


def _project_to_row(project: dict[str, Any]) -> dict[str, Any]:
    project_id = str(project["gid"])
    title = project.get("name") or f"Asana project {project_id}"
    lines = [f"# {title}", "", "Project details:"]
    _append_metadata(lines, "Owner", _name(project.get("owner")))
    _append_metadata(lines, "Team", _name(project.get("team")))
    _append_metadata(lines, "Workspace", _name(project.get("workspace")))
    _append_metadata(lines, "Archived", project.get("archived"))
    _append_metadata(lines, "Created", project.get("created_at"))
    _append_metadata(lines, "Modified", project.get("modified_at"))
    notes = project.get("notes") or ""
    if notes:
        lines.extend(("", "## Description", "", notes))

    return {
        "id": f"project:{project_id}",
        "external_id": project_id,
        "kind": "project",
        "title": title,
        "url": project.get("permalink_url") or "",
        "content": "\n".join(lines).strip(),
        "modified_at": project.get("modified_at") or "",
        "_deleted": False,
    }


def _task_to_row(task: dict[str, Any], comments: Sequence[dict[str, Any]]) -> dict[str, Any]:
    task_id = str(task["gid"])
    parent = task.get("parent") or {}
    kind = "subtask" if parent.get("gid") else "task"
    title = task.get("name") or f"Asana {kind} {task_id}"
    lines = [f"# {title}", "", f"Asana {kind} details:"]
    _append_metadata(lines, "Status", "completed" if task.get("completed") else "incomplete")
    _append_metadata(lines, "Assignee", _name(task.get("assignee")))
    _append_metadata(lines, "Parent task", parent.get("name") or parent.get("gid"))
    _append_metadata(lines, "Created", task.get("created_at"))
    _append_metadata(lines, "Modified", task.get("modified_at"))
    _append_metadata(lines, "Completed", task.get("completed_at"))
    _append_metadata(lines, "Start", task.get("start_at") or task.get("start_on"))
    _append_metadata(lines, "Due", task.get("due_at") or task.get("due_on"))

    memberships = task.get("memberships") or []
    project_names = [
        _name(membership.get("project"))
        for membership in memberships
        if _name(membership.get("project"))
    ]
    section_names = [
        _name(membership.get("section"))
        for membership in memberships
        if _name(membership.get("section"))
    ]
    tags = [_name(tag) for tag in task.get("tags") or [] if _name(tag)]
    _append_metadata(lines, "Projects", ", ".join(project_names))
    _append_metadata(lines, "Sections", ", ".join(section_names))
    _append_metadata(lines, "Tags", ", ".join(tags))

    custom_fields = [
        f"{field.get('name')}: {field.get('display_value')}"
        for field in task.get("custom_fields") or []
        if field.get("name") and field.get("display_value") not in (None, "")
    ]
    if custom_fields:
        lines.extend(("", "## Custom fields", "", *[f"- {item}" for item in custom_fields]))

    notes = task.get("notes") or ""
    if notes:
        lines.extend(("", "## Description", "", notes))

    if comments:
        lines.extend(("", "## Comments", ""))
        for story in comments:
            author = _name(story.get("created_by")) or "Unknown author"
            created_at = story.get("created_at") or "unknown time"
            lines.append(f"- {author} ({created_at}): {story['text']}")

    return {
        "id": f"task:{task_id}",
        "external_id": task_id,
        "kind": kind,
        "title": title,
        "url": task.get("permalink_url") or "",
        "content": "\n".join(lines).strip(),
        "modified_at": task.get("modified_at") or "",
        "_deleted": False,
    }


def _deleted_row(row_id: str) -> dict[str, Any]:
    return {"id": row_id, "_deleted": True}


def sync_asana(
    client: Any,
    state: dict[str, Any],
    *,
    project_ids: Sequence[str] | None = None,
    workspace_id: str | None = None,
) -> Iterator[dict[str, Any]]:
    """Yield new/changed Asana documents and hard-delete tombstones.

    State is committed only after every API read succeeds and the iterator is
    fully consumed. A failed inventory or comment poll therefore cannot advance
    the cursor or accidentally turn a partial response into mass deletion.
    """
    selected_projects = _resolve_project_ids(client, project_ids, workspace_id)
    known_ids = set(state.get("known_ids", []))
    previous_versions: dict[str, str] = dict(state.get("task_versions", {}))
    previous_story_fingerprints: dict[str, str] = dict(state.get("story_fingerprints", {}))
    previous_project_fingerprints: dict[str, str] = dict(state.get("project_fingerprints", {}))
    last_modified_at = str(state.get("last_modified_at") or "")

    projects: dict[str, dict[str, Any]] = {}
    missing_projects: list[str] = []
    for project_id in selected_projects:
        try:
            project = _project_details(client, project_id)
        except AsanaNotFoundError:
            missing_projects.append(project_id)
            continue
        projects[str(project["gid"])] = project

    # A selected project that disappears after a successful sync is a real
    # upstream deletion. On a first run, however, every project returning 404
    # almost certainly means a typo; fail visibly instead of silently loading
    # an empty dataset.
    if missing_projects and not known_ids and not projects:
        raise AsanaNotFoundError(
            "None of the selected Asana projects exists or is accessible: "
            + ", ".join(missing_projects)
        )

    active_project_ids = list(projects)
    tasks = _discover_tasks(client, active_project_ids)

    changed_task_ids: set[str] = set()
    if last_modified_at:
        for project_id in active_project_ids:
            changed_task_ids.update(_changed_project_task_ids(client, project_id, last_modified_at))

    current_ids = {f"project:{project_id}" for project_id in projects}
    current_ids.update(f"task:{task_id}" for task_id in tasks)

    next_project_fingerprints: dict[str, str] = {}
    for project_id, project in projects.items():
        row = _project_to_row(project)
        fingerprint = _fingerprint(row)
        next_project_fingerprints[project_id] = fingerprint
        if previous_project_fingerprints.get(project_id) != fingerprint:
            yield row

    next_versions: dict[str, str] = {}
    next_story_fingerprints: dict[str, str] = {}
    newest_modified_at = last_modified_at
    changed_count = 0

    for task_id in sorted(tasks):
        stub = tasks[task_id]
        modified_at = str(stub.get("modified_at") or "")
        next_versions[task_id] = modified_at
        if modified_at > newest_modified_at:
            newest_modified_at = modified_at

        # Poll comments for every current task. This is deliberately separate
        # from modified_since because a comment-only change may not move the
        # task cursor on every Asana API representation.
        comments = _task_comments(client, task_id)
        story_fingerprint = _story_fingerprint(comments)
        next_story_fingerprints[task_id] = story_fingerprint

        is_new = f"task:{task_id}" not in known_ids
        version_changed = previous_versions.get(task_id) != modified_at
        comments_changed = previous_story_fingerprints.get(task_id) != story_fingerprint
        if not (is_new or task_id in changed_task_ids or version_changed or comments_changed):
            continue

        yield _task_to_row(_task_details(client, task_id), comments)
        changed_count += 1

    deleted_ids = sorted(known_ids - current_ids)
    for row_id in deleted_ids:
        yield _deleted_row(row_id)

    state["known_ids"] = sorted(current_ids)
    state["last_modified_at"] = newest_modified_at
    state["task_versions"] = next_versions
    state["story_fingerprints"] = next_story_fingerprints
    state["project_fingerprints"] = next_project_fingerprints

    logger.info(
        "Asana: synced %d changed task(s), %d project(s), %d missing project(s), "
        "and %d deletion(s).",
        changed_count,
        len(projects),
        len(missing_projects),
        len(deleted_ids),
    )


def asana_source(
    token: str | None = None,
    *,
    project_ids: Sequence[str] | None = None,
    workspace_id: str | None = None,
    client: Any = None,
):
    """Create a dlt source for selected Asana projects.

    Args:
        token: Asana personal access token. Falls back to
            ``ASANA_ACCESS_TOKEN``. Not needed when ``client`` is injected.
        project_ids: Explicit project GIDs to ingest.
        workspace_id: Ingest every accessible project in this workspace. Pass
            this or ``project_ids``, not both.
        client: Pre-built requests-compatible session, primarily for tests.

    Returns:
        A document-mode dlt source suitable for ``cognee.remember``.
    """
    try:
        import dlt
    except ImportError as exc:  # pragma: no cover - optional dependency guard
        raise ImportError(
            'Install the Asana connector with: pip install "cognee-community-connector-asana"'
        ) from exc

    if isinstance(project_ids, str):
        raise TypeError("project_ids must be a sequence of project GIDs, not a string.")
    if bool(project_ids) == bool(workspace_id):
        raise ValueError("Select exactly one of project_ids or workspace_id.")

    resolved_client = client
    if resolved_client is None:
        resolved_token = token or os.environ.get("ASANA_ACCESS_TOKEN")
        if not resolved_token:
            raise ValueError("Asana token required: pass token= or set ASANA_ACCESS_TOKEN.")
        resolved_client = _make_session(resolved_token)

    @dlt.resource(
        name=ASANA_TABLE_NAME,
        primary_key="id",
        write_disposition="merge",
        columns={"_deleted": {"data_type": "bool", "hard_delete": True}},
    )
    def asana_documents():
        resource_state = dlt.current.resource_state()
        yield from sync_asana(
            resolved_client,
            resource_state,
            project_ids=project_ids,
            workspace_id=workspace_id,
        )

    @dlt.source(name=ASANA_SOURCE_NAME)
    def _asana():
        return asana_documents

    source = _asana()
    setattr(source, DOCUMENT_SOURCE_ATTR, ASANA_SOURCE_NAME)
    return source
