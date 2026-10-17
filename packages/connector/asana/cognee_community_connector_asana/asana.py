"""Asana connector for cognee: a ``dlt`` source that turns Asana projects into memory.

Pulls tasks (with their comments and subtasks) and project descriptions
from Asana, incrementally and with forget-on-delete. The source built here is
handed directly to :func:`cognee.remember`::

    import cognee
    from cognee_community_connector_asana import asana_source

    await cognee.remember(
        asana_source(project_gids=["1201234567890123"]),  # ASANA_ACCESS_TOKEN from env
        dataset_name="asana",
        primary_key="id",
        write_disposition="merge",   # incremental upsert by row id
        max_rows_per_table=0,        # 0 = no row cap
    )

Design
------
* **Auth**: a personal access token, sent as ``Authorization: Bearer``. The
  connector only issues ``GET`` requests.
* **Documents**: one per task (``task:<gid>``) and one per selected project
  (``project:<gid>``). A task that sits in two selected projects is still one
  document. The source declares ``cognee_document_source = "asana"``, so every
  row goes through normal cognify instead of the relational dlt path.
* **Incremental sync**: two signals per project, both kept in dlt resource state.
  ``GET /tasks?modified_since=<cursor>`` finds tasks whose own fields changed or
  that gained or lost a comment. The listing starts 60 seconds before the
  cursor, so a task stamped at (or just before) the cursor after the previous
  listing ran is not missed; tasks already seen in that window are skipped.
  The Events API sync token finds what ``modified_since`` cannot see: editing
  an existing comment, and changing or deleting a subtask, leave the task's
  ``modified_at`` untouched. Renaming the project or one of its sections
  touches no task at all, yet every task document names both, so that event
  re-renders the whole project.
* **Events gap**: Asana answers ``412`` with a fresh token when there is no
  token yet or the stored one is too old (about a day). Whatever happened in
  between is unknowable, so that run re-renders every task of that project.
  Unchanged text keeps its content hash, so nothing is re-cognified.
* **Forget-on-delete**: every run lists the gids currently in each project and
  compares them with the ids emitted by earlier runs. Ids that vanished are
  emitted with the ``_deleted`` hard-delete marker; dlt removes those rows on
  ``merge`` and cognee's ``orphan_cleanup`` purges them from memory.
* **Safety**: every listing is finished before the first row is yielded, and
  state is written only after the last one. A listing that fails therefore
  deletes nothing and moves no cursor; the next run starts from the same point.
  A task that fails on its own keeps its document and is retried next run; a
  ``401``, an exhausted rate limit or several failures in a row abort.
* **Several sources in one dataset**: ``resource_name`` names the dlt table and
  the sync state. Give each token or selection its own name.
"""

from __future__ import annotations

import os
import time
from collections.abc import Callable, Iterable, Iterator
from datetime import datetime, timedelta
from typing import Any

from cognee.shared.logging_utils import get_logger
from cognee.tasks.ingestion import dlt_utils
from cognee.tasks.ingestion.dlt_utils import DOCUMENT_SOURCE_ATTR

logger = get_logger("asana_connector")

# Default dlt resource / staging-table name, and the document-source tag for its rows.
ASANA_TABLE_NAME = "asana_documents"
ASANA_SOURCE_NAME = "asana"

_API_BASE = "https://app.asana.com/api/1.0"
# Per request: a generous bound for one API call, so a stalled connection fails and is retried.
_TIMEOUT_SECONDS = 30
_PAGE_SIZE = 100  # Asana's maximum ``limit`` for every listing used here

# Retry budget for rate-limited / transient responses: five attempts with
# 1, 2, 4, 8 s back-off between them, or exactly Asana's Retry-After on a 429.
_MAX_RETRIES = 5
# 429 is Asana's rate limit (150 requests a minute on the free plan); the 5xx
# answers are the ones Asana's docs describe as temporary.
_TRANSIENT_STATUSES = (429, 500, 502, 503, 504)
# How often a listing is restarted from its first page after an offset expired.
# Offsets expire (Asana's docs); one restart normally suffices, two leave slack.
_MAX_LISTING_RESTARTS = 2
# How far before the cursor the modified_since listing starts (see sync_tasks).
# The overlap only has to catch a task stamped at or just before the cursor
# that the previous listing could not see yet; a minute is a wide margin.
_CURSOR_OVERLAP_SECONDS = 60
# Upper bound on chained /events pages in one run (guards against has_more
# loops). Far more than one project produces between runs; past it the run
# treats the feed as a gap and re-renders the project.
_MAX_EVENT_PAGES = 100
# Abort when this many tasks in a row fail on their own. One bad task is kept
# and retried next run, but a streak means Asana itself is failing, and every
# further task would spend five retries for nothing.
_MAX_FAILURES_IN_A_ROW = 3

# Asana returns compact objects unless fields are requested, and wide requests
# count against its cost quota, so each call asks only for what is rendered
# (plus created_at on stories, to put comments in order).
_TASK_FIELDS = (
    "name,notes,completed,due_on,permalink_url,assignee.name,"
    "memberships.project.name,memberships.section.name,"
    "custom_fields.name,custom_fields.display_value"
)
_STORY_FIELDS = "resource_subtype,text,created_by.name,created_at"
_SUBTASK_FIELDS = "name,completed,assignee.name,due_on,notes"
_PROJECT_FIELDS = "name,notes,permalink_url"
# resource.target is what identifies the task of an edited comment (see
# _event_task_gids); change.field tells a rename from other edits (see _is_rename).
_EVENT_FIELDS = (
    "action,parent.gid,parent.resource_type,resource.gid,resource.resource_type,"
    "resource.target.gid,resource.target.resource_type,change.field"
)


class AsanaAPIError(RuntimeError):
    """A non-retryable (or retries-exhausted) error response from the Asana API."""

    def __init__(self, status: int, message: str):
        super().__init__(f"Asana API returned {status}: {message}")
        self.status = status


class AsanaListingError(RuntimeError):
    """Asana answered ``200`` with a body that is not JSON or not of the expected shape."""


class AsanaUnavailableError(RuntimeError):
    """Too many tasks in a row failed: Asana is failing, not one task."""


class AsanaSyncStoppedError(RuntimeError):
    """The host's ``check_active`` raised an ``OSError``; the sync must stop.

    Network errors are ``OSError`` too and are retried, or kept per task, so an
    ``OSError`` from the host's check is re-raised as this type, which nothing
    in the connector catches. Other exceptions from the check pass unchanged.
    """


# ---------------------------------------------------------------------------
# Auth / HTTP helpers
# ---------------------------------------------------------------------------
def _make_session(token: str) -> Any:
    """Build a ``requests`` session authenticated with an Asana access token."""
    try:
        import requests
    except ImportError as exc:  # pragma: no cover - requests is a declared dependency
        raise ImportError(
            'The Asana connector requires "requests". Install the package:\n'
            "    pip install cognee-community-connector-asana"
        ) from exc

    session = requests.Session()
    session.headers.update({"Authorization": f"Bearer {token}", "Accept": "application/json"})
    return session


def _get(
    session: Any, path: str, params: dict | None = None, ok: tuple[int, ...] = (200,)
) -> tuple[int, dict]:
    """GET an Asana API path and return ``(status, json)``, retrying transient errors.

    Rate limits (429), server errors (5xx) and network failures are retried up
    to ``_MAX_RETRIES`` times. Any other status outside ``ok`` raises
    :class:`AsanaAPIError` so the caller decides what it means.
    """
    url = f"{_API_BASE}{path}"
    for attempt in range(_MAX_RETRIES):
        last_attempt = attempt == _MAX_RETRIES - 1
        try:
            response = session.get(url, params=params or {}, timeout=_TIMEOUT_SECONDS)
        except OSError as exc:
            # requests' connection and timeout errors all derive from OSError.
            if last_attempt:
                raise
            delay = float(2**attempt)
            logger.warning("Asana: %s; retrying in %.1fs.", exc, delay)
            time.sleep(delay)
            continue

        status = response.status_code
        if status in ok:
            return status, _json_object(response)
        if status in _TRANSIENT_STATUSES and not last_attempt:
            delay = _retry_delay(response.headers, attempt)
            logger.warning("Asana: HTTP %d on %s; retrying in %.1fs.", status, path, delay)
            time.sleep(delay)
            continue
        raise AsanaAPIError(status, _error_message(response))

    raise AssertionError("unreachable: the loop returns or raises")  # pragma: no cover


def _json_object(response: Any) -> dict:
    """The JSON object Asana wraps every answer in; anything else raises.

    An empty or odd body must never be read as an empty listing, which would
    look like deletions.
    """
    try:
        body = response.json()
    except ValueError as exc:
        raise AsanaListingError(f"Asana answered {response.status_code} without JSON") from exc
    if not isinstance(body, dict):
        raise AsanaListingError(
            f"Asana answered {response.status_code} with a {type(body).__name__}, not an object"
        )
    return body


def _data(body: dict, shape: type) -> Any:
    """``body["data"]``, which must be a ``list`` or a ``dict`` as expected."""
    data = body.get("data")
    if not isinstance(data, shape):
        raise AsanaListingError(
            f"Asana's data is a {type(data).__name__} where a {shape.__name__} was expected"
        )
    return data


def _retry_delay(headers: Any, attempt: int) -> float:
    """Seconds to wait before a retry: exactly ``Retry-After`` when Asana sends it."""
    try:
        return float((headers or {}).get("Retry-After"))
    except (TypeError, ValueError):
        return float(2**attempt)


def _error_message(response: Any) -> str:
    """Extract Asana's ``errors[0].message``, falling back to the raw body."""
    try:
        return response.json()["errors"][0]["message"]
    except (ValueError, KeyError, IndexError, TypeError):
        return str(getattr(response, "text", ""))[:200]


def _list_all(session: Any, path: str, params: dict) -> list[dict]:
    """Return every item of a paginated collection. Never a partial list.

    Follows only the ``offset`` Asana hands back. Offsets expire, and an expired
    one is rejected with 400; because the same request without an offset just
    succeeded, a 400 on a later page can only be about the offset, so the
    listing restarts from the first page. Any other failure propagates.
    """
    for restart in range(_MAX_LISTING_RESTARTS + 1):
        items: list[dict] = []
        page_params = {**params, "limit": _PAGE_SIZE}
        try:
            while True:
                _, body = _get(session, path, page_params)
                items.extend(_data(body, list))
                offset = (body.get("next_page") or {}).get("offset")
                if not offset:
                    return items
                page_params = {**params, "limit": _PAGE_SIZE, "offset": offset}
        except AsanaAPIError as exc:
            expired_offset = exc.status == 400 and "offset" in page_params
            if not expired_offset or restart == _MAX_LISTING_RESTARTS:
                raise
            logger.warning("Asana: pagination offset rejected on %s; restarting listing.", path)

    raise AssertionError("unreachable: the loop returns or raises")  # pragma: no cover


# ---------------------------------------------------------------------------
# Asana API reads
# ---------------------------------------------------------------------------
def _list_tasks(
    session: Any,
    project_gid: str,
    fields: str,
    include_completed: bool,
    modified_since: str | None = None,
) -> list[dict]:
    """List a project's tasks, optionally only those modified since a timestamp."""
    params: dict[str, Any] = {"project": project_gid, "opt_fields": fields}
    if modified_since:
        params["modified_since"] = modified_since
    if not include_completed:
        # Asana's idiom for "incomplete tasks only".
        params["completed_since"] = "now"
    return _list_all(session, "/tasks", params)


def _overlap_start(cursor: str) -> str:
    """Return the timestamp ``_CURSOR_OVERLAP_SECONDS`` before ``cursor``.

    Formatted like Asana's own ``modified_at`` (UTC, milliseconds, ``Z``) so the
    result can be sent as ``modified_since`` and compared as a string.
    """
    if not cursor:
        return ""
    start = datetime.fromisoformat(cursor) - timedelta(seconds=_CURSOR_OVERLAP_SECONDS)
    return start.strftime("%Y-%m-%dT%H:%M:%S.") + f"{start.microsecond // 1000:03d}Z"


def _workspace_project_gids(session: Any, workspace_gid: str) -> list[str]:
    """Return the gid of every project in a workspace."""
    params = {"workspace": workspace_gid, "opt_fields": "gid"}
    return [project["gid"] for project in _list_all(session, "/projects", params)]


def _poll_events(
    session: Any, project_gid: str, sync_token: str | None
) -> tuple[list[dict] | None, str]:
    """Return ``(events, next_sync_token)`` for a project since ``sync_token``.

    ``events`` is ``None`` when the feed has a gap: Asana answers 412 (with a
    fresh token in the body) when no token is sent or the stored one is too
    old. The caller must then assume anything may have changed.
    """
    events: list[dict] = []
    for _ in range(_MAX_EVENT_PAGES):
        params = {"resource": project_gid, "opt_fields": _EVENT_FIELDS}
        if sync_token:
            params["sync"] = sync_token
        status, body = _get(session, "/events", params, ok=(200, 412))
        sync_token = body.get("sync")
        if not sync_token:
            raise AsanaAPIError(status, "events response carried no sync token")
        if status == 412:
            return None, sync_token
        events.extend(_data(body, list))
        if not body.get("has_more"):
            return events, sync_token

    # Still "more" after the page budget: treat it like a gap rather than guess.
    return None, sync_token


# ---------------------------------------------------------------------------
# Events -> tasks
# ---------------------------------------------------------------------------
def _event_task_gids(events: Iterable[dict]) -> set[str]:
    """Return the gids of the tasks a batch of project events is about.

    * A story (comment) event names its task as ``parent`` when the comment was
      added or removed. When a comment is *edited*, ``parent`` is null and only
      ``resource.target`` says which task it belongs to.
    * A task event whose ``parent`` is a task means a subtask was added to or
      removed from that parent, so the parent is the one to re-render.
    * Any other task event (renamed, completed, deleted, ...) is about the task
      named in ``resource``.

    The result may contain subtask gids and gids of deleted tasks;
    ``_documents_for`` maps them to task documents or drops them.
    """
    gids: set[str] = set()
    for event in events:
        resource = event.get("resource") or {}
        parent = event.get("parent") or {}
        kind = resource.get("resource_type")
        if kind == "story":
            owner = parent if parent.get("resource_type") == "task" else resource.get("target")
            if owner and owner.get("resource_type") == "task":
                gids.add(owner["gid"])
        elif kind == "task":
            gids.add(parent["gid"] if parent.get("resource_type") == "task" else resource["gid"])
    return gids


def _is_rename(events: Iterable[dict]) -> bool:
    """True when the project or one of its sections was renamed.

    Every task document carries its project and section names, and a rename
    bumps no task's ``modified_at``, so the only way to refresh those documents
    is to re-render the project. Other project edits (its description, colour,
    dates) do not appear in task documents and are ignored here.
    """
    return any(
        (event.get("resource") or {}).get("resource_type") in ("project", "section")
        and event.get("action") == "changed"
        and (event.get("change") or {}).get("field") == "name"
        for event in events
    )


def _documents_for(
    task_gids: set[str], swept: set[str], subtask_parents: dict[str, str]
) -> set[str]:
    """Map event task gids onto the task documents they affect.

    A gid in ``swept`` (the tasks currently in the selected projects) is a
    document itself. Otherwise it may be a subtask whose title is folded into
    its parent's document; ``subtask_parents`` remembers, from earlier renders,
    which document that is. The map is needed because Asana offers no other
    route: renaming or deleting a subtask does not touch the parent, the event
    carries no parent, and a deleted subtask can no longer be asked for one.
    Anything else (a deleted task, a deeper subtask, another project) is dropped.
    """
    documents: set[str] = set()
    for gid in task_gids:
        if gid in swept:
            documents.add(gid)
        elif subtask_parents.get(gid) in swept:
            documents.add(subtask_parents[gid])
    return documents


# ---------------------------------------------------------------------------
# Rendering (pure)
# ---------------------------------------------------------------------------
def _task_id(gid: str) -> str:
    return f"task:{gid}"


def _project_id(gid: str) -> str:
    return f"project:{gid}"


def _render_task(task: dict, comments: list[dict], subtasks: list[dict]) -> str:
    """Render a task, its comments and its subtasks as one text document.

    The text is deterministic for unchanged input and carries no timestamps or
    counters, so an unchanged task keeps its content hash and is not
    re-cognified. Every list is sorted (projects, custom fields by name,
    subtasks by creation, comments by time), so the same task renders the same
    text whatever order Asana lists its parts in. The task name is not repeated
    here; it is the row's title.
    """
    # "Completed", not "Status": workspaces commonly have a custom field named Status.
    lines = [f"Completed: {'yes' if task.get('completed') else 'no'}"]

    assignee = (task.get("assignee") or {}).get("name")
    if assignee:
        lines.append(f"Assignee: {assignee}")
    if task.get("due_on"):
        lines.append(f"Due: {task['due_on']}")
    lines.extend(_membership_lines(task.get("memberships") or []))
    fields = sorted(
        (field["name"], field["display_value"])
        for field in task.get("custom_fields") or []
        if field.get("name") and field.get("display_value")
    )
    lines.extend(f"{name}: {value}" for name, value in fields)

    sections = ["\n".join(lines)]
    notes = (task.get("notes") or "").strip()
    if notes:
        sections.append(notes)
    if subtasks:
        ordered = sorted(subtasks, key=_gid_order)
        lines = [line for subtask in ordered for line in _subtask_lines(subtask)]
        sections.append("Subtasks:\n" + "\n".join(lines))
    if comments:
        ordered = sorted(
            comments, key=lambda story: (story.get("created_at") or "", _gid_order(story))
        )
        sections.append("Comments:\n" + "\n".join(_comment_line(story) for story in ordered))
    return "\n\n".join(sections)


def _gid_order(item: dict) -> tuple[int, str]:
    """Creation order of Asana objects: gids are numeric strings that grow over time."""
    gid = str(item.get("gid") or "")
    return len(gid), gid


def _membership_lines(memberships: list[dict]) -> list[str]:
    """One ``Project: X (section: Y)`` line per project the task is in, sorted."""
    lines = []
    for membership in memberships:
        project = (membership.get("project") or {}).get("name")
        section = (membership.get("section") or {}).get("name")
        if project:
            lines.append(
                f"Project: {project} (section: {section})" if section else f"Project: {project}"
            )
    return sorted(lines)


def _subtask_lines(subtask: dict) -> list[str]:
    """A checkbox line for a subtask, then indented details that are set."""
    checked = "x" if subtask.get("completed") else " "
    lines = [f"- [{checked}] {(subtask.get('name') or '').strip()}"]

    details = []
    assignee = (subtask.get("assignee") or {}).get("name")
    if assignee:
        details.append(f"Assignee: {assignee}")
    if subtask.get("due_on"):
        details.append(f"Due: {subtask['due_on']}")
    notes = (subtask.get("notes") or "").strip()
    if notes:
        details.extend(f"Notes: {notes}".splitlines())
    return lines + [f"  {detail}" for detail in details]


def _comment_line(story: dict) -> str:
    author = (story.get("created_by") or {}).get("name") or "Unknown"
    return f"- {author}: {(story.get('text') or '').strip()}"


def _deleted_row(row_id: str) -> dict[str, Any]:
    """Build a minimal row that instructs dlt to hard-delete a document by id."""
    return {"id": row_id, "_deleted": True}


def _fetch_task(session: Any, task_gid: str) -> tuple[dict, list[dict], list[dict]]:
    """Fetch ``(task, comments, subtasks)`` for one task. Errors propagate.

    The task was in this run's listing, so a ``403`` or ``404`` here does not
    prove it is gone (it may be a permission or a passing glitch); the caller
    keeps it and asks again next run, and the listing decides deletions.
    """
    _, body = _get(session, f"/tasks/{task_gid}", {"opt_fields": _TASK_FIELDS})
    task = _data(body, dict)
    stories = _list_all(session, f"/tasks/{task_gid}/stories", {"opt_fields": _STORY_FIELDS})
    subtasks = _list_all(session, f"/tasks/{task_gid}/subtasks", {"opt_fields": _SUBTASK_FIELDS})
    # The stories feed also holds system entries ("added to project", ...).
    comments = [story for story in stories if story.get("resource_subtype") == "comment_added"]
    return task, comments, subtasks


class _ItemFailures:
    """The tasks of one run that failed on their own and are kept for a retry.

    Only per-task failures are kept: an :class:`AsanaAPIError`, an
    :class:`AsanaListingError` or an ``OSError`` that outlasted the retries. A
    ``401`` or a ``429`` that outlasted them would fail every later task the
    same way, so it is re-raised, and so is the ``_MAX_FAILURES_IN_A_ROW``-th
    failure in a row. Anything else (``check_active``'s errors among them) is
    never caught in the first place.
    """

    def __init__(self) -> None:
        self.in_a_row = 0
        self.kept: set[str] = set()

    def keep(self, exc: Exception, task_gid: str) -> None:
        """Count a per-task failure and log it, or raise if it is systemic."""
        if isinstance(exc, AsanaAPIError) and exc.status in (401, 429):
            raise exc
        self.in_a_row += 1
        endpoint = f"/tasks/{task_gid}"
        if self.in_a_row >= _MAX_FAILURES_IN_A_ROW:
            raise AsanaUnavailableError(
                f"{self.in_a_row} tasks in a row failed, the last on {endpoint}; stopping"
            ) from exc
        what = f"HTTP {exc.status}" if isinstance(exc, AsanaAPIError) else type(exc).__name__
        logger.warning("Asana: kept %s for a retry next run (%s).", endpoint, what)
        self.kept.add(task_gid)

    def succeeded(self) -> None:
        self.in_a_row = 0


# What a per-task request can fail with once the retries are spent.
_ITEM_ERRORS = (AsanaAPIError, AsanaListingError, OSError)


def _task_row(
    task_gid: str, task: dict, comments: list[dict], subtasks: list[dict]
) -> dict[str, Any]:
    """Flatten a fetched task into a dlt row."""
    return {
        "id": _task_id(task_gid),
        "url": task.get("permalink_url") or "",
        "title": (task.get("name") or "").strip(),
        "content": _render_task(task, comments, subtasks),
        "_deleted": False,
    }


def _project_row(session: Any, project_gid: str) -> dict[str, Any]:
    """Fetch a project and flatten its description into a dlt row."""
    _, body = _get(session, f"/projects/{project_gid}", {"opt_fields": _PROJECT_FIELDS})
    project = _data(body, dict)
    return {
        "id": _project_id(project_gid),
        "url": project.get("permalink_url") or "",
        "title": (project.get("name") or "").strip(),
        "content": (project.get("notes") or "").strip(),
        "_deleted": False,
    }


# ---------------------------------------------------------------------------
# Sync (pure given a session + state dict, so it is unit-testable)
# ---------------------------------------------------------------------------
def sync_tasks(
    session: Any,
    state: dict,
    *,
    project_gids: list[str],
    include_completed: bool = True,
) -> Iterator[dict[str, Any]]:
    """Yield changed documents since the last run, plus hard-delete markers.

    ``state`` holds ``known_ids`` (every row id emitted so far), ``subtasks``
    (subtask gid -> the task document its title is folded into), ``retry``
    (tasks that failed to render last time) and, per project, a
    ``modified_since`` ``cursor``, the tasks ``seen`` close to that cursor, and
    an Events ``sync`` token.

    The work is split so that a failure can never be mistaken for a deletion:

    1. *List.* For every project: poll events, list the gids currently in it
       (the sweep), and list tasks modified since the cursor. Any error here
       propagates before a single row is yielded.
    2. *Render.* Fetch and yield each changed task once, then each project.
       A task that fails on its own keeps its old document and is retried.
    3. *Delete.* Yield a hard-delete marker for every known id that is no
       longer current.
    4. *Commit.* Only now write the new id set, cursors and tokens to ``state``.
    """
    known_ids: set[str] = set(state.get("known_ids", []))
    subtask_parents: dict[str, str] = dict(state.get("subtasks", {}))
    previous: dict = state.get("projects", {})
    failures = _ItemFailures()

    next_projects: dict[str, dict[str, Any]] = {}
    swept: set[str] = set()  # task gids currently in the selected projects
    changed: set[str] = set()  # task gids to (re-)render
    event_gids: set[str] = set()

    # --- 1. List ------------------------------------------------------------
    for project_gid in project_gids:
        project_state: dict = previous.get(project_gid) or {}
        cursor: str = project_state.get("cursor", "")
        # Events are polled first: the new token then covers everything that
        # happens while the listings below are running.
        events, sync_token = _poll_events(session, project_gid, project_state.get("sync"))
        # No cursor yet (first sync of this project) or a gap in the event feed:
        # there is no way to know what changed, so every task is re-rendered.
        # The same goes for a renamed project or section, which changes the text
        # of every task in it without touching any of them.
        full = events is None or not cursor or _is_rename(events)

        sweep = {
            task["gid"] for task in _list_tasks(session, project_gid, "gid", include_completed)
        }
        # The listing starts a little before the cursor. A task can carry the
        # cursor's exact timestamp, or one just before it, and still not have
        # been in the previous listing (it changed in the same millisecond
        # after that listing ran, or was not visible to it yet).
        listed = _list_tasks(
            session,
            project_gid,
            "modified_at",
            include_completed,
            modified_since=None if full else _overlap_start(cursor),
        )

        # ``seen`` is what the previous run saw inside that overlap window
        # (gid -> modified_at). It tells an unchanged task, which is skipped,
        # from one that is new to the window or has moved, which is rendered.
        seen: dict[str, str] = project_state.get("seen", {})
        stamps = {task["gid"]: task.get("modified_at") or "" for task in listed}
        changed.update(gid for gid, when in stamps.items() if full or seen.get(gid) != when)
        # A task new to the corpus is rendered whatever its timestamp says.
        changed.update(gid for gid in sweep if _task_id(gid) not in known_ids)

        event_gids |= _event_task_gids(events or [])
        swept |= sweep
        newest = max([cursor, *stamps.values()])
        window_start = _overlap_start(newest)
        next_projects[project_gid] = {
            "cursor": newest,
            "sync": sync_token,
            "seen": {gid: when for gid, when in stamps.items() if when >= window_start},
        }

    changed |= _documents_for(event_gids, swept, subtask_parents)
    # Tasks that failed last time are tried again, whatever their timestamps say.
    changed |= set(state.get("retry", []))
    # Only tasks in the sweep are rendered, so every emitted id ends up in
    # known_ids and can be deleted by a later run. A retry entry for a task that
    # left the sweep is dropped here; the sweep's own deletion applies to it.
    changed &= swept

    # --- 2. Render ----------------------------------------------------------
    current_ids = {_task_id(gid) for gid in swept}
    rendered = 0
    for task_gid in sorted(changed):
        try:
            task, comments, subtasks = _fetch_task(session, task_gid)
        except _ITEM_ERRORS as exc:
            failures.keep(exc, task_gid)
            # No row, so the old document stays; its id is in current_ids, so
            # it is not deleted, and state["retry"] brings it back next run.
            # A task that was never emitted gets its first document then; until
            # that, its id in known_ids is harmless (a hard delete of a row that
            # does not exist is a no-op).
            continue
        failures.succeeded()
        # Replace what is remembered about this task's subtasks with what was
        # just rendered, so a later event about one of them finds this document.
        subtask_parents = {c: p for c, p in subtask_parents.items() if p != task_gid}
        subtask_parents.update((subtask["gid"], task_gid) for subtask in subtasks)
        rendered += 1
        yield _task_row(task_gid, task, comments, subtasks)

    # Project descriptions are cheap (one request each) and emitted every run;
    # an unchanged description keeps its content hash downstream.
    for project_gid in project_gids:
        row = _project_row(session, project_gid)
        current_ids.add(row["id"])
        yield row

    # --- 3. Delete ----------------------------------------------------------
    deleted = known_ids - current_ids
    for row_id in sorted(deleted):
        yield _deleted_row(row_id)

    # --- 4. Commit ----------------------------------------------------------
    state["known_ids"] = sorted(current_ids)
    state["subtasks"] = {
        child: parent
        for child, parent in sorted(subtask_parents.items())
        if _task_id(parent) in current_ids
    }
    for project_state in next_projects.values():
        # A kept task must not look settled inside the overlap window.
        project_state["seen"] = {
            gid: when for gid, when in project_state["seen"].items() if gid not in failures.kept
        }
    state["projects"] = next_projects
    state["retry"] = sorted(failures.kept)
    logger.info(
        "Asana: %d task(s) rendered, %d project(s), %d deletion(s); kept %d task(s) for retry.",
        rendered,
        len(project_gids),
        len(deleted),
        len(failures.kept),
    )


# ---------------------------------------------------------------------------
# Public factory
# ---------------------------------------------------------------------------
def asana_source(
    token: str | None = None,
    project_gids: list[str] | None = None,
    workspace_gid: str | None = None,
    include_completed: bool = True,
    session: Any = None,
    *,
    resource_name: str = ASANA_TABLE_NAME,
    check_active: Callable[[], None] | None = None,
):
    """Return a ``dlt`` resource that yields Asana documents for ``remember``.

    Args:
        token: Asana personal access token. Falls back to ``ASANA_ACCESS_TOKEN``.
        project_gids: The projects to ingest. Takes precedence over
            ``workspace_gid``.
        workspace_gid: Ingest every project of this workspace. Used only when
            ``project_gids`` is omitted.
        include_completed: When ``False`` only incomplete tasks are synced, and
            a task is forgotten once it is completed.
        session: Pre-built ``requests`` session. Mainly an injection point for
            tests; when omitted one is built from the token.
        resource_name: Stable dlt resource (table) name, which also scopes the
            sync state. Give each token or project selection its own name when
            several sync into one dataset; sharing a name would make each run
            forget the other's documents.
        check_active: Optional host authorization checkpoint. It is called
            before every Asana request and around every yielded row, and stops
            the sync by raising; nothing is deleted and no state moves.

    Returns:
        A ``dlt`` resource (``resource_name``) configured with
        ``primary_key="id"``, ``write_disposition="merge"`` and an ``_deleted``
        hard-delete column. Hand it to ``cognee.remember(...)``.
    """
    try:
        import dlt
    except ImportError as exc:  # pragma: no cover - dlt is a declared dependency
        raise ImportError(
            'The Asana connector requires "dlt". Install the package:\n'
            "    pip install cognee-community-connector-asana"
        ) from exc

    if getattr(dlt_utils, "DOCUMENT_SYNC_VERSION", 0) < 1:
        raise RuntimeError(
            "The Asana connector requires a cognee build with table-scoped dlt document "
            "cleanup (cognee>=1.6). Upgrade cognee so upstream deletions are forgotten."
        )

    if isinstance(project_gids, str):
        project_gids = [project_gids]
    if not project_gids and not workspace_gid:
        raise ValueError("asana_source requires project_gids=[...] or workspace_gid=...")

    resolved_token = token or os.environ.get("ASANA_ACCESS_TOKEN")
    if session is None and not resolved_token:
        raise ValueError("Asana access token required: pass token= or set ASANA_ACCESS_TOKEN.")

    @dlt.resource(
        name=resource_name,
        primary_key="id",
        write_disposition="merge",
        # _deleted is a boolean hard-delete marker: rows where it is True are
        # removed from the dlt destination on merge, which propagates the
        # deletion through cognee's orphan_cleanup.
        columns={"_deleted": {"data_type": "bool", "hard_delete": True}},
    )
    def asana_documents():
        client = session or _make_session(resolved_token)
        check = _stop_on(check_active) if check_active is not None else None
        if check is not None:
            # The listing phase makes many requests before the first row.
            client = _GuardedSession(client, check)
        yield from dlt_utils.guarded_rows(_rows(client), check)

    def _rows(client: Any) -> Iterator[dict[str, Any]]:
        if project_gids:
            # De-duplicate while keeping the caller's order.
            selected = list(dict.fromkeys(str(gid) for gid in project_gids))
        else:
            selected = _workspace_project_gids(client, str(workspace_gid))
        yield from sync_tasks(
            client,
            dlt.current.resource_state(),
            project_gids=selected,
            include_completed=include_completed,
        )

    resource = asana_documents()
    # Opt into the document ingestion path (row -> text document -> cognify).
    # resolve_dlt_sources reads this marker; it never imports this connector.
    setattr(resource, DOCUMENT_SOURCE_ATTR, ASANA_SOURCE_NAME)
    # Give the incremental state its own dlt pipeline per dataset, so syncing
    # other dlt sources (or another dataset) cannot disturb cursors and tokens.
    setattr(resource, dlt_utils.PIPELINE_SCOPE_ATTR, resource_name)
    return resource


def _stop_on(check_active: Callable[[], None]) -> Callable[[], None]:
    """``check_active``, turning an ``OSError`` it raises into ``AsanaSyncStoppedError``."""

    def check() -> None:
        try:
            check_active()
        except OSError as exc:
            raise AsanaSyncStoppedError(f"check_active stopped the sync: {exc!r}") from exc

    return check


class _GuardedSession:
    """A session that asks the host's ``check_active`` before every request."""

    def __init__(self, session: Any, check_active: Callable[[], None]):
        self._session = session
        self._check_active = check_active

    def get(self, *args: Any, **kwargs: Any) -> Any:
        self._check_active()
        return self._session.get(*args, **kwargs)
