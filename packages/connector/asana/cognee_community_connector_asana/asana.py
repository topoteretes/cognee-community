"""Asana connector for cognee: a ``dlt`` source that turns Asana projects into memory.

One document per task (with its comments and subtasks) and one per selected
project, synced incrementally with forget-on-delete. Hand the source to
:func:`cognee.remember`::

    await cognee.remember(
        asana_source(project_gids=["1201234567890123"]),  # ASANA_ACCESS_TOKEN from env
        dataset_name="asana",
        primary_key="id",
        write_disposition="merge",
        max_rows_per_table=0,
    )

The README describes what is stored, how changes and deletions are found, and
what a run does when Asana fails part of the way through.
"""

from __future__ import annotations

import os
import time
from collections.abc import Callable, Iterable, Iterator
from datetime import UTC, datetime, timedelta
from typing import Any

import dlt
import requests
from cognee.shared.logging_utils import get_logger
from cognee.tasks.ingestion import dlt_utils
from cognee.tasks.ingestion.dlt_utils import DOCUMENT_SOURCE_ATTR
from dlt.common.normalizers.naming.snake_case import NamingConvention

logger = get_logger("asana_connector")

# Default dlt resource / staging-table name, and the document-source tag for its rows.
ASANA_TABLE_NAME = "asana_documents"
ASANA_SOURCE_NAME = "asana"

_API_BASE = "https://app.asana.com/api/1.0"
# Per request: a generous bound for one API call, so a stalled connection fails and is retried.
_TIMEOUT_SECONDS = 30
_PAGE_SIZE = 100  # Asana's maximum ``limit`` for every listing used here

# Five attempts per request, with 1, 2, 4, 8 s between them, or Asana's
# Retry-After on a 429.
_MAX_RETRIES = 5
# 429 is Asana's rate limit (150 requests a minute on the free plan); the 5xx
# answers are the ones Asana's docs describe as temporary.
_TRANSIENT_STATUSES = (429, 500, 502, 503, 504)
# The longest single wait, and the most a run waits in all. cognee holds its dlt
# staging lock during a sync, so a longer Retry-After, or a wait past the
# budget, stops the run cleanly instead (it resumes next run).
_MAX_WAIT_SECONDS = 60
_MAX_TOTAL_WAIT_SECONDS = 120
# How often a listing is restarted from its first page after an offset expired.
# Offsets expire (Asana's docs); one restart normally suffices, two leave slack.
_MAX_LISTING_RESTARTS = 2
# Pages per listing before it is treated as broken: 10,000 pages of 100 is far
# beyond any project, and stops a listing that never ends.
_MAX_LIST_PAGES = 10_000
# How far before the cursor the modified-since window starts (see _Run._list).
# It only has to catch a task stamped at or just before the cursor that the
# previous run could not see yet; a minute is a wide margin.
_CURSOR_OVERLAP_SECONDS = 60
# Upper bound on chained /events pages in one run. Far more than one project
# produces between runs; past it the run treats the feed as a gap.
_MAX_EVENT_PAGES = 100
# Fresh task failures in a row (5xx or no answer, after retries) that mean Asana
# itself is down: the run stops cleanly. One or two can be a single broken task;
# three different tasks failing back to back are rarely a coincidence, and a stop
# costs nothing, as the next run resumes. Tasks already kept for a retry never count.
_OUTAGE_STREAK = 3
# A listed task that answers 403/404 at the same modified_at is forgotten after
# 3 runs AND 24 hours, counting only runs that finished and showed access to
# work (see _Run.settle): a host on a fast cron cannot forget a task in minutes,
# and a weekly cron still needs three observations. A forgotten task is asked
# again when its modified_at moves, or after another day.
_GIVE_UP_RUNS = 3
_GIVE_UP_AFTER = timedelta(hours=24)
_REASK_AFTER = timedelta(hours=24)
# Forgotten tasks asked again per run, oldest first, after everything else.
_MAX_REASKS = 10
# Where a forgotten task from older state, with no time of its own, sits.
_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)

# Asana returns compact objects unless fields are requested, and wide requests
# count against its cost quota, so each call asks only for what is rendered
# (plus created_at on stories, to put comments in order).
_TASK_FIELDS = (
    "name,notes,completed,due_on,permalink_url,assignee.name,"
    "memberships.project.gid,memberships.project.name,memberships.section.name,"
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

_STAT_KEYS = ("scanned", "skipped", "failed", "deleted", "kept", "given_up")


class AsanaAPIError(RuntimeError):
    """An error answer from Asana (``status``), or no answer at all (``status`` 0).

    The message names only the status and the endpoint. Asana's own explanation
    is on ``detail``, with the token cut out; a response body never is.
    """

    def __init__(self, status: int, endpoint: str, detail: str = ""):
        answer = f"HTTP {status}" if status else "no answer"
        super().__init__(f"Asana {endpoint}: {answer}")
        self.status = status
        self.endpoint = endpoint
        self.detail = detail


class AsanaResponseError(RuntimeError):
    """Asana answered ``200`` with a body that is not JSON or not of the expected shape.

    Never read as an empty listing or a skippable task: it aborts the run.
    """


class _ExpiredOffsetError(Exception):
    """Internal: Asana rejected the offset of a later page (it expired)."""

    def __init__(self, api_error: AsanaAPIError):
        super().__init__(str(api_error))
        self.api_error = api_error


class _CutShortError(Exception):
    """Internal: stop the run cleanly, keeping what was rendered so far."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------
def _make_session(token: str) -> requests.Session:
    """A ``requests`` session that sends the token as ``Authorization: Bearer``."""
    session = requests.Session()
    session.headers.update({"Authorization": f"Bearer {token}", "Accept": "application/json"})
    return session


def _check_token(token: str) -> None:
    """Refuse a token ``requests`` would reject, without ever echoing it.

    A pasted token often ends in a newline; requests then raises an error whose
    message holds the whole header, token included.
    """
    if any(char.isspace() or not char.isprintable() for char in token):
        raise ValueError("The Asana token contains whitespace or control characters")


class _Client:
    """One run's access to Asana: the session, the host's check, and the waits."""

    def __init__(
        self, session: Any, check_active: Callable[[], None] | None = None, secret: str = ""
    ):
        self.session = session
        self.check_active = check_active
        # The token, only to cut it out of Asana's error messages.
        headers = getattr(session, "headers", None)
        bearer = headers.get("Authorization", "") if headers else ""
        self.secret = secret or bearer.removeprefix("Bearer ")
        self.waited = 0.0  # seconds slept this run
        self.out_of_time = False  # a wait was refused for the run's budget

    def check(self) -> None:
        if self.check_active is not None:
            self.check_active()

    def can_wait(self, seconds: float) -> bool:
        """Whether a wait fits in the single-wait cap and the run's budget."""
        if seconds > _MAX_WAIT_SECONDS:
            return False
        if self.waited + seconds > _MAX_TOTAL_WAIT_SECONDS:
            self.out_of_time = True
            return False
        return True

    def wait(self, seconds: float) -> None:
        self.check()
        self.waited += seconds
        time.sleep(seconds)


def _as_client(client: Any) -> _Client:
    return client if isinstance(client, _Client) else _Client(client)


def _get(
    client: Any, path: str, params: dict | None = None, ok: tuple[int, ...] = (200,)
) -> tuple[int, dict]:
    """GET an Asana API path and return ``(status, json)``, retrying transient errors.

    Rate limits (429), server errors (5xx) and network failures are retried up
    to ``_MAX_RETRIES`` times; a wait over ``_MAX_WAIT_SECONDS``, or past the
    run's ``_MAX_TOTAL_WAIT_SECONDS``, is not taken. Every other status outside
    ``ok`` raises :class:`AsanaAPIError`.
    """
    client = _as_client(client)
    url = f"{_API_BASE}{path}"
    attempt = 0
    while True:
        last_attempt = attempt == _MAX_RETRIES - 1
        client.check()
        try:
            response = client.session.get(url, params=params or {}, timeout=_TIMEOUT_SECONDS)
        except requests.RequestException as exc:
            # Only the type: the message of some requests errors holds a header.
            delay = float(2**attempt)
            if last_attempt or not client.can_wait(delay):
                raise AsanaAPIError(0, path, type(exc).__name__) from None
            logger.warning("Asana: %s on %s; retrying in %.0fs.", type(exc).__name__, path, delay)
            client.wait(delay)
            attempt += 1
            continue

        status = response.status_code
        if status in ok:
            return status, _json_object(response, path)
        if status in _TRANSIENT_STATUSES and not last_attempt:
            delay = _retry_delay(response.headers, attempt)
            if not client.can_wait(delay):
                raise AsanaAPIError(status, path, f"asked to wait {delay:.0f}s")
            logger.warning("Asana: HTTP %d on %s; retrying in %.0fs.", status, path, delay)
            client.wait(delay)
            attempt += 1
            continue
        raise AsanaAPIError(status, path, _redact(_error_detail(response), client.secret))


def _json_object(response: Any, path: str) -> dict:
    """The JSON object Asana wraps every answer in; anything else raises."""
    try:
        body = response.json()
    except ValueError:
        status = response.status_code
        raise AsanaResponseError(f"Asana {path}: HTTP {status} without JSON") from None
    if not isinstance(body, dict):
        raise AsanaResponseError(f"Asana {path}: a {type(body).__name__}, not an object")
    return body


def _data(body: dict, shape: type, path: str) -> Any:
    """``body["data"]``, which must be a ``list`` or a ``dict`` as expected."""
    data = body.get("data")
    if not isinstance(data, shape):
        raise AsanaResponseError(
            f"Asana {path}: data is a {type(data).__name__}, not a {shape.__name__}"
        )
    return data


def _retry_delay(headers: Any, attempt: int) -> float:
    """Seconds to wait before a retry: ``Retry-After`` when Asana sends it."""
    try:
        return float((headers or {}).get("Retry-After"))
    except (TypeError, ValueError):
        return float(2**attempt)


def _error_detail(response: Any) -> str:
    """Asana's ``errors[0].message``, or nothing. Never the raw body."""
    try:
        message = response.json()["errors"][0]["message"]
    except (ValueError, KeyError, IndexError, TypeError):
        return ""
    return message[:200] if isinstance(message, str) else ""


def _redact(text: str, secret: str) -> str:
    """``text`` with the token cut out, in case Asana echoes it back.

    A token too short to replace safely (it could match ordinary words) drops
    the whole text when it occurs in it.
    """
    if not secret or secret not in text:
        return text
    return text.replace(secret, "[token]") if len(secret) >= 8 else ""


def _list_all(client: Any, path: str, params: dict) -> list[dict]:
    """Return every item of a paginated collection. Never a partial list.

    An expired offset is rejected with 400; since the same request without an
    offset just succeeded, a 400 on a later page can only be the offset, so the
    listing restarts from the first page. Any other failure propagates.
    """
    client = _as_client(client)
    restarts = 0
    while True:
        try:
            return _list_pages(client, path, params)
        except _ExpiredOffsetError as exc:
            if restarts == _MAX_LISTING_RESTARTS:
                raise exc.api_error from None
            restarts += 1
            logger.warning("Asana: pagination offset rejected on %s; restarting listing.", path)


def _list_pages(client: _Client, path: str, params: dict) -> list[dict]:
    items: list[dict] = []
    offsets: set[str] = set()
    page_params = {**params, "limit": _PAGE_SIZE}
    for _ in range(_MAX_LIST_PAGES):
        try:
            _, body = _get(client, path, page_params)
        except AsanaAPIError as exc:
            if exc.status == 400 and "offset" in page_params:
                raise _ExpiredOffsetError(exc) from None
            raise
        items.extend(_data(body, list, path))
        offset = (body.get("next_page") or {}).get("offset")
        if not offset:
            return items
        if offset in offsets:
            raise AsanaResponseError(f"Asana {path}: a pagination offset came back twice")
        offsets.add(offset)
        page_params = {**params, "limit": _PAGE_SIZE, "offset": offset}
    raise AsanaResponseError(f"Asana {path}: more than {_MAX_LIST_PAGES} pages")


# ---------------------------------------------------------------------------
# Asana API reads
# ---------------------------------------------------------------------------
def _list_tasks(client: _Client, project_gid: str, include_completed: bool) -> list[dict]:
    """Every task in a project, with its ``modified_at``."""
    params: dict[str, Any] = {"project": project_gid, "opt_fields": "modified_at"}
    if not include_completed:
        # Asana's idiom for "incomplete tasks only".
        params["completed_since"] = "now"
    return _list_all(client, "/tasks", params)


def _overlap_start(cursor: str) -> str:
    """The timestamp ``_CURSOR_OVERLAP_SECONDS`` before ``cursor``, in Asana's format."""
    if not cursor:
        return ""
    start = datetime.fromisoformat(cursor) - timedelta(seconds=_CURSOR_OVERLAP_SECONDS)
    return start.strftime("%Y-%m-%dT%H:%M:%S.") + f"{start.microsecond // 1000:03d}Z"


def _workspace_project_gids(client: Any, workspace_gid: str) -> list[str]:
    """The gid of every project in a workspace."""
    params = {"workspace": workspace_gid, "opt_fields": "gid"}
    projects = _list_all(client, "/projects", params)
    return [project["gid"] for project in projects if project.get("gid")]


def _poll_events(
    client: _Client, project_gid: str, sync_token: str | None
) -> tuple[list[dict] | None, str]:
    """Return ``(events, next_sync_token)`` for a project since ``sync_token``.

    ``events`` is ``None`` when the feed has a gap: Asana answers 412 (with a
    fresh token) when no token is sent or the stored one is too old, and a feed
    longer than ``_MAX_EVENT_PAGES`` is treated the same way.
    """
    events: list[dict] = []
    for _ in range(_MAX_EVENT_PAGES):
        params = {"resource": project_gid, "opt_fields": _EVENT_FIELDS}
        if sync_token:
            params["sync"] = sync_token
        status, body = _get(client, "/events", params, ok=(200, 412))
        sync_token = body.get("sync")
        if not sync_token:
            raise AsanaResponseError("Asana /events: no sync token")
        if status == 412:
            return None, sync_token
        events.extend(_data(body, list, "/events"))
        if not body.get("has_more"):
            return events, sync_token
    return None, sync_token


# ---------------------------------------------------------------------------
# Events -> tasks
# ---------------------------------------------------------------------------
def _event_task_gids(events: Iterable[dict]) -> set[str]:
    """Return the gids of the tasks a batch of project events is about.

    * A story (comment) event names its task as ``parent`` when the comment was
      added or removed; when it is *edited*, only ``resource.target`` does.
    * A task event whose ``parent`` is a task means a subtask was added to or
      removed from that parent, so the parent is the one to re-render.
    * Any other task event is about the task named in ``resource``.

    Events without a gid are skipped. The result may hold subtask gids and gids
    of deleted tasks; ``_documents_for`` maps or drops them.
    """
    gids: set[str] = set()
    for event in events:
        resource = event.get("resource") or {}
        parent = event.get("parent") or {}
        kind = resource.get("resource_type")
        owner: dict = {}
        if kind == "story":
            owner = parent if parent.get("resource_type") == "task" else resource.get("target")
            if not owner or owner.get("resource_type") != "task":
                owner = {}
        elif kind == "task":
            owner = parent if parent.get("resource_type") == "task" else resource
        if owner.get("gid"):
            gids.add(owner["gid"])
    return gids


def _is_rename(events: Iterable[dict]) -> bool:
    """True when the project or one of its sections was renamed.

    Every task document carries its project and section names, and a rename
    bumps no task's ``modified_at``, so the project is re-rendered.
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

    A gid in ``swept`` is a document itself. Otherwise it may be a subtask whose
    title is folded into its parent's document; ``subtask_parents`` remembers
    which, because renaming or deleting a subtask touches neither the parent nor
    the event's ``parent`` field. Anything else is dropped.
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


def _render_task(
    task: dict, comments: list[dict], subtasks: list[dict], projects: Iterable[str] | None = None
) -> str:
    """Render a task, its comments and its subtasks as one text document.

    Deterministic for unchanged input, with no timestamps or counters, and every
    list sorted, so an unchanged task keeps its content hash. ``projects`` (the
    selected project gids) limits the membership lines to projects that are
    synced, whose renames are therefore seen. The task name is the row's title.
    """
    # "Completed", not "Status": workspaces commonly have a custom field named Status.
    lines = [f"Completed: {'yes' if task.get('completed') else 'no'}"]

    assignee = (task.get("assignee") or {}).get("name")
    if assignee:
        lines.append(f"Assignee: {assignee}")
    if task.get("due_on"):
        lines.append(f"Due: {task['due_on']}")
    lines.extend(_membership_lines(task.get("memberships") or [], projects))
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


def _membership_lines(memberships: list[dict], projects: Iterable[str] | None) -> list[str]:
    """One ``Project: X (section: Y)`` line per selected project the task is in, sorted."""
    selected = None if projects is None else set(projects)
    lines = []
    for membership in memberships:
        project = membership.get("project") or {}
        if selected is not None and project.get("gid") not in selected:
            continue
        section = (membership.get("section") or {}).get("name")
        if project.get("name"):
            name = project["name"]
            lines.append(f"Project: {name} (section: {section})" if section else f"Project: {name}")
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


def _fetch_task(client: _Client, task_gid: str) -> tuple[dict, list[dict], list[dict]]:
    """Fetch ``(task, comments, subtasks)`` for one task. Errors propagate."""
    path = f"/tasks/{task_gid}"
    _, body = _get(client, path, {"opt_fields": _TASK_FIELDS})
    task = _data(body, dict, path)
    stories = _list_all(client, f"{path}/stories", {"opt_fields": _STORY_FIELDS})
    subtasks = _list_all(client, f"{path}/subtasks", {"opt_fields": _SUBTASK_FIELDS})
    # The stories feed also holds system entries ("added to project", ...).
    comments = [story for story in stories if story.get("resource_subtype") == "comment_added"]
    return task, comments, subtasks


def _task_row(
    task_gid: str, task: dict, comments: list[dict], subtasks: list[dict], projects: list[str]
) -> dict[str, Any]:
    """Flatten a fetched task into a dlt row."""
    return {
        "id": _task_id(task_gid),
        "url": task.get("permalink_url") or "",
        "title": (task.get("name") or "").strip(),
        "content": _render_task(task, comments, subtasks, projects),
        "_deleted": False,
    }


def _project_row(client: _Client, project_gid: str) -> dict[str, Any]:
    """Fetch a project and flatten its description into a dlt row."""
    path = f"/projects/{project_gid}"
    _, body = _get(client, path, {"opt_fields": _PROJECT_FIELDS})
    project = _data(body, dict, path)
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
class _Run:
    """One sync over the selected projects; ``state`` changes only at the end.

    State: ``known_ids`` (row ids emitted and current), ``subtasks`` (subtask gid
    -> the task document it is folded into), ``retry`` (tasks kept after a
    failure), ``pending`` (tasks a stopped run did not reach), ``unreadable``
    (gid -> ``{stamp, runs, since}`` of tasks answering 403/404, ``since`` being
    the run clock at the first counted run), ``given_up`` (gid -> ``{stamp, at}``:
    the stamp at which an unreadable task was forgotten, and when it was last
    asked) and, per project, the ``cursor``, the tasks ``seen`` near it and the
    Events ``sync`` token.
    """

    def __init__(
        self, client: _Client, state: dict, stats: dict[str, int], now: datetime | None = None
    ):
        self.client = client
        self.state = state
        self.stats = stats
        self.now = now or _run_clock()
        self.known_ids: set[str] = set(state.get("known_ids", []))
        self.subtask_parents: dict[str, str] = dict(state.get("subtasks", {}))
        # parent -> its subtasks, so replacing one task's entries is not a full scan
        self.children: dict[str, list[str]] = {}
        for child, parent in self.subtask_parents.items():
            self.children.setdefault(parent, []).append(child)
        self.unreadable = _load_unreadable(state.get("unreadable", {}), self.now)
        self.given_up = _load_given_up(state.get("given_up", {}))
        # What a run that stops early commits instead: only finished runs count.
        self.unreadable_before = {gid: dict(entry) for gid, entry in self.unreadable.items()}
        self.retry: set[str] = set(state.get("retry", []))
        self.projects: list[str] = []
        self.next_projects: dict[str, dict[str, Any]] = {}
        self.stamps: dict[str, str] = {}  # gid -> modified_at, every swept task
        self.order: list[str] = []  # tasks to render: fresh, retries, then re-asks
        self.position = 0  # index in ``order`` of the next task to render
        self.kept: set[str] = set()
        self.rendered: set[str] = set()
        self.outage = 0
        self.unread: list[str] = []  # tasks that answered 403/404 this run
        self.counted: set[str] = set()  # tasks whose 403/404 counted this run
        self.reasks: set[str] = set()  # forgotten tasks asked again this run
        self.reask_failed: set[str] = set()
        self.listed = False

    # -- 1. List ---------------------------------------------------------------
    def list(self, project_gids: list[str], include_completed: bool) -> None:
        self.projects = project_gids
        previous: dict = self.state.get("projects", {})
        changed: set[str] = set()
        event_gids: set[str] = set()
        for project_gid in project_gids:
            project_state: dict = previous.get(project_gid) or {}
            cursor: str = project_state.get("cursor", "")
            # Events first: the new token then covers what happens during the listing.
            events, sync_token = _poll_events(self.client, project_gid, project_state.get("sync"))
            # No cursor yet, a gap in the feed, or a renamed project or section:
            # anything may have changed, so every task is re-rendered.
            full = events is None or not cursor or _is_rename(events)
            listed = {
                task["gid"]: task.get("modified_at") or ""
                for task in _list_tasks(self.client, project_gid, include_completed)
                if task.get("gid")
            }
            # The window starts a little before the cursor: a task can carry the
            # cursor's timestamp, or one just before it, and still have been
            # missed by the previous run. ``seen`` (gid -> modified_at inside the
            # previous window) tells a task already rendered from one that moved.
            start = _overlap_start(cursor)
            seen: dict[str, str] = project_state.get("seen", {})
            changed.update(
                gid
                for gid, when in listed.items()
                if full or (when >= start and seen.get(gid) != when)
            )
            # A task new to the corpus is rendered whatever its timestamp says.
            changed.update(gid for gid in listed if _task_id(gid) not in self.known_ids)
            event_gids |= _event_task_gids(events or [])
            self.stamps.update(listed)
            newest = max([cursor, *listed.values()])
            window = _overlap_start(newest)
            self.next_projects[project_gid] = {
                "cursor": newest,
                "sync": sync_token,
                "seen": {gid: when for gid, when in listed.items() if when >= window},
            }

        swept = set(self.stamps)
        changed |= _documents_for(event_gids, swept, self.subtask_parents)
        changed |= set(self.state.get("pending", []))
        # A forgotten unreadable task is asked again once its stamp moves, or after a day.
        for gid, entry in list(self.given_up.items()):
            if gid not in swept:
                del self.given_up[gid]
            elif self.stamps[gid] == entry["stamp"]:
                changed.discard(gid)
                self.retry.discard(gid)
            else:
                del self.given_up[gid]
                changed.add(gid)
        due = sorted(
            (entry["at"], gid)
            for gid, entry in self.given_up.items()
            if _parse(entry["at"]) <= self.now - _REASK_AFTER
        )
        self.reasks = {gid for _, gid in due[:_MAX_REASKS]}
        # Only swept tasks are rendered, so every emitted id can be deleted later.
        self.retry &= swept
        fresh = (changed & swept) - self.retry
        self.order = sorted(fresh) + sorted(self.retry) + sorted(self.reasks)
        self.stats["scanned"] = len(swept)
        resting = len(self.given_up) - len(self.reasks)
        self.stats["skipped"] = len(swept) - len(self.order) - resting
        self.listed = True

    # -- 2. Render -------------------------------------------------------------
    def render(self) -> Iterator[dict[str, Any]]:
        for index, gid in enumerate(self.order):
            self.position = index
            try:
                task, comments, subtasks = _fetch_task(self.client, gid)
            except AsanaAPIError as exc:
                self._failed(gid, exc)
                continue
            if gid not in self.retry | self.reasks:
                self.outage = 0
            if self.given_up.pop(gid, None) is not None:
                logger.info("Asana: /tasks/%s is readable again.", gid)
            self.unreadable.pop(gid, None)
            self._remember_subtasks(gid, subtasks)
            self.rendered.add(gid)
            yield _task_row(gid, task, comments, subtasks, self.projects)
        self.position = len(self.order)
        self.settle()
        # One request each; an unchanged description keeps its content hash downstream.
        for project_gid in self.projects:
            yield _project_row(self.client, project_gid)

    def _failed(self, gid: str, exc: AsanaAPIError) -> None:
        """Keep a task that failed on its own, or stop the run if Asana as a whole is failing."""
        self.stats["failed"] += 1
        self.stop_if_systemic(exc)
        if exc.status == 401:
            raise exc  # nothing rendered yet: the token is bad
        if gid in self.reasks:
            # Still unreadable: it stays forgotten and is asked again in a day.
            self.reask_failed.add(gid)
            return
        self.kept.add(gid)
        logger.warning("Asana: kept /tasks/%s for a retry next run (%s).", gid, exc)
        if exc.status in (403, 404):
            # Still listed, so not gone: hidden from this token, or a passing
            # failure. settle() decides which once the run's fetches are done.
            self.unread.append(gid)
        elif gid not in self.retry:
            self.outage += 1
            if self.outage >= _OUTAGE_STREAK:
                raise _CutShortError("unavailable")

    def settle(self) -> None:
        """Count this run's 403/404s where access to tasks is shown to work.

        A 403 or 404 on a listed task means it is hidden from this token, or that
        Asana is failing or the token lost a permission; only the first is a
        reason to forget it. So it counts only if another task was read in this
        run. In steady state the others are unchanged and not fetched, so one of
        them is fetched as a probe; if the probe fails too, nothing counts.
        """
        pending, self.unread = self.unread, []
        if not pending:
            return
        readable = bool(self.rendered)
        probe = None if readable else self._probe()
        if readable or probe:
            for gid in pending:
                self._count_unreadable(gid)
            return
        if probe is None:
            return  # Asana is failing, which proves nothing either way
        self.stats["failed_permission"] = self.stats.get("failed_permission", 0) + len(pending)
        if self._probe_gid() is None:
            logger.warning(
                "Asana: task: every fetch answered 403/404 this run and there is no other "
                "task to check access with; nothing counts toward forgetting it."
            )
        else:
            logger.warning(
                "Asana: task: every fetch answered 403/404 this run; check the token's permissions."
            )

    def _probe(self) -> bool | None:
        """Fetch one unchanged task: True when it is readable.

        False when it answers 403/404 too, or there is no other task to try;
        None when Asana is failing (5xx, no answer), which proves nothing.
        """
        gid = self._probe_gid()
        if gid is None:
            return False
        self.stats["probe_task"] = 1
        path = f"/tasks/{gid}"
        try:
            _, body = _get(self.client, path, {"opt_fields": "gid"})
        except AsanaAPIError as exc:
            self.stop_if_systemic(exc)
            if exc.status == 401:
                raise exc
            return False if exc.status in (403, 404) else None
        _data(body, dict, path)
        return True

    def _probe_gid(self) -> str | None:
        """The newest task not fetched this run and not unreadable or forgotten."""
        unusable = set(self.order) | self.unreadable.keys() | self.given_up.keys()
        gids = [gid for gid in self.stamps if gid not in unusable]
        if not gids:
            return None
        return max(gids, key=lambda gid: (self.stamps[gid], _gid_order({"gid": gid})))

    def _count_unreadable(self, gid: str) -> None:
        entry = self.unreadable.get(gid)
        stamp = self.stamps.get(gid, "")
        if entry and entry["stamp"] == stamp:
            self.unreadable[gid] = {**entry, "runs": entry["runs"] + 1}
        else:
            self.unreadable[gid] = {"stamp": stamp, "runs": 1, "since": _stamp(self.now)}
        self.counted.add(gid)

    def stop_if_systemic(self, exc: AsanaAPIError) -> None:
        if self.client.out_of_time:
            raise _CutShortError("rate_limit" if exc.status == 429 else "unavailable") from None
        if exc.status == 429:
            raise _CutShortError("rate_limit") from None
        if exc.status == 401 and self.rendered:
            raise _CutShortError("auth") from None

    def _remember_subtasks(self, gid: str, subtasks: list[dict]) -> None:
        """Replace this task's subtask entries, so a later subtask event finds its document."""
        for child in self.children.pop(gid, []):
            if self.subtask_parents.get(child) == gid:
                del self.subtask_parents[child]
        children = [subtask["gid"] for subtask in subtasks if subtask.get("gid")]
        for child in children:
            self.subtask_parents[child] = gid
        self.children[gid] = children

    # -- 3. Delete and 4. Commit ------------------------------------------------
    def finish(self) -> Iterator[dict[str, Any]]:
        for gid, entry in self.unreadable.items():
            if (
                gid in self.counted
                and gid in self.kept
                and entry["runs"] >= _GIVE_UP_RUNS
                and _parse(entry["since"]) <= self.now - _GIVE_UP_AFTER
            ):
                logger.warning(
                    "Asana: /tasks/%s answered HTTP 403/404 in %d runs since %s; its document "
                    "is forgotten until the task changes or reads again.",
                    gid,
                    entry["runs"],
                    entry["since"],
                )
                self.given_up[gid] = {"stamp": entry["stamp"], "at": _stamp(self.now)}
                self.kept.discard(gid)
                self.stats["given_up"] += 1
        for gid in self.reask_failed:
            self.given_up[gid]["at"] = _stamp(self.now)
        current_ids = {_task_id(gid) for gid in self.stamps if gid not in self.given_up}
        current_ids |= {_project_id(gid) for gid in self.projects}
        deleted = sorted(self.known_ids - current_ids)
        for row_id in deleted:
            yield _deleted_row(row_id)
        self.stats["deleted"] = len(deleted)
        self._commit(current_ids, [], self.kept, self.unreadable)

    def commit_progress(self) -> None:
        """After a clean stop: keep what was rendered, queue what was not, delete nothing.

        Nothing this run saw counts toward giving a task up. A forgotten task that
        was read again, has changed or is gone is no longer forgotten (as in any
        run); nothing new is forgotten, and none is marked as asked. A re-ask not
        reached is queued like any task; the next run asks it only if it is due.
        """
        if not self.listed:
            return  # stopped while listing: nothing moved
        rest = self.order[self.position :]
        retry = self.kept | (self.retry & set(rest))
        pending = [gid for gid in rest if gid not in retry]
        current_ids = self.known_ids | {_task_id(gid) for gid in self.rendered}
        unreadable = {g: e for g, e in self.unreadable_before.items() if g not in self.rendered}
        self._commit(current_ids, pending, retry, unreadable)

    def _commit(
        self, current_ids: set[str], pending: list[str], retry: set[str], unreadable: dict
    ) -> None:
        unsettled = retry | set(pending)
        for project_state in self.next_projects.values():
            # A task not rendered must not look settled inside the overlap window.
            project_state["seen"] = {
                gid: when for gid, when in project_state["seen"].items() if gid not in unsettled
            }
        self.state["known_ids"] = sorted(current_ids)
        self.state["subtasks"] = {
            child: parent
            for child, parent in sorted(self.subtask_parents.items())
            if _task_id(parent) in current_ids
        }
        self.state["projects"] = self.next_projects
        self.state["retry"] = sorted(retry)
        self.state["pending"] = pending
        self.state["unreadable"] = {
            gid: entry for gid, entry in sorted(unreadable.items()) if gid in unsettled
        }
        self.state["given_up"] = dict(sorted(self.given_up.items()))
        self.stats["kept"] = len(retry)


def _load_unreadable(saved: dict, now: datetime) -> dict[str, dict]:
    """``unreadable`` from state. An entry from before ``since`` existed starts its day now."""
    return {gid: {"since": _stamp(now), **entry} for gid, entry in saved.items()}


def _load_given_up(saved: dict) -> dict[str, dict]:
    """``given_up`` from state. An entry that is only a stamp (older state) is due at once."""
    return {
        gid: dict(entry) if isinstance(entry, dict) else {"stamp": entry, "at": _stamp(_EPOCH)}
        for gid, entry in saved.items()
    }


def _parse(stamp: str) -> datetime:
    """Parse a timestamp such as ``2026-10-09T19:14:27.123Z``."""
    return datetime.fromisoformat(stamp)


def _stamp(moment: datetime) -> str:
    """Format a moment the way Asana does (UTC, milliseconds, ``Z``)."""
    moment = moment.astimezone(UTC)
    return moment.strftime("%Y-%m-%dT%H:%M:%S.") + f"{moment.microsecond // 1000:03d}Z"


def _run_clock() -> datetime:
    """The run's clock, for the give-up and re-ask times.

    The local one: no Asana answer this connector reads carries the server's
    time in a header it uses, and a few minutes of drift do not matter on a
    scale of a day.
    """
    return _local_now()


def _local_now() -> datetime:
    return datetime.now(UTC)


def sync_tasks(
    session: Any,
    state: dict,
    *,
    project_gids: list[str],
    include_completed: bool = True,
    check_active: Callable[[], None] | None = None,
    stats: dict[str, int] | None = None,
    now: datetime | None = None,
) -> Iterator[dict[str, Any]]:
    """Yield changed documents since the last run, plus hard-delete markers.

    1. *List.* Per project: poll events and list every task with its
       ``modified_at``. A failure here propagates before a single row.
    2. *Render.* Each changed task (fresh first, then retries, then forgotten ones
       due to be asked again), then each project.
    3. *Delete.* A hard-delete marker for every known id that is no longer current.
    4. *Commit.* Only now is ``state`` written.

    A run stopped cleanly (a ``401`` after progress, a rate limit, an outage,
    the wait budget spent) keeps what it rendered, queues the rest in ``pending``
    and deletes nothing. ``now`` (for tests) defaults to the run clock.
    """
    if not project_gids:
        raise ValueError("No Asana project selected; an empty selection would forget everything")
    stats = {} if stats is None else stats
    for key in _STAT_KEYS:
        stats.setdefault(key, 0)
    client = session if isinstance(session, _Client) else _Client(session, check_active)
    run = _Run(client, state, stats, now)
    try:
        try:
            run.list(project_gids, include_completed)
            yield from run.render()
            yield from run.finish()
        except AsanaAPIError as exc:
            run.stop_if_systemic(exc)  # also a rate limit while listing, or on a project
            raise
    except _CutShortError as cut:
        run.commit_progress()
        stats[f"failed_{cut.reason}"] = 1
        logger.warning("Asana sync stopped early (%s); it resumes on the next run.", cut.reason)
        return
    logger.info(
        "Asana: %d task(s) rendered, %d project(s), %d deletion(s); kept %d task(s) for retry.",
        len(run.rendered),
        len(project_gids),
        stats["deleted"],
        stats["kept"],
    )


# ---------------------------------------------------------------------------
# Public factory
# ---------------------------------------------------------------------------
def asana_source(
    token: str | None = None,
    project_gids: list[str] | None = None,
    workspace_gid: str | None = None,
    include_completed: bool = True,
    service: Any = None,
    *,
    resource_name: str = ASANA_TABLE_NAME,
    check_active: Callable[[], None] | None = None,
    session: Any = None,
):
    """Return a ``dlt`` resource that yields Asana documents for ``remember``.

    Args:
        token: Asana personal access token. Falls back to ``ASANA_ACCESS_TOKEN``.
        project_gids: The projects to ingest. Takes precedence over ``workspace_gid``.
        workspace_gid: Ingest every project of this workspace.
        include_completed: When ``False`` only incomplete tasks are synced, and a
            task is forgotten once it is completed.
        service: Pre-built ``requests``-like session, mainly for tests; when
            omitted one is built from the token for each run and closed after it.
            ``session`` is accepted as an alias.
        resource_name: dlt resource (table) name, which also scopes the sync
            state. Give each token or selection its own name in a shared dataset.
        check_active: Optional host authorization checkpoint, called before every
            request and wait and around every row; it stops the sync by raising.

    Returns:
        A ``dlt`` resource with ``primary_key="id"``, ``write_disposition="merge"``
        and a ``_deleted`` hard-delete column. ``resource.cognee_sync_stats``
        holds the last run's counts.
    """
    if not (hasattr(dlt_utils, "PIPELINE_SCOPE_ATTR") and hasattr(dlt_utils, "guarded_rows")):
        raise RuntimeError(
            "The Asana connector needs cognee's dlt_utils.PIPELINE_SCOPE_ATTR and "
            "guarded_rows (cognee>=1.6.2)."
        )
    _check_resource_name(resource_name)
    if service is not None and session is not None:
        raise ValueError("Pass service= or session=, not both")
    service = service if service is not None else session

    if isinstance(project_gids, str):
        project_gids = [project_gids]
    if not project_gids and not workspace_gid:
        raise ValueError("asana_source requires project_gids=[...] or workspace_gid=...")

    resolved_token = token or os.environ.get("ASANA_ACCESS_TOKEN") or ""
    if service is None:
        if not resolved_token:
            raise ValueError("Asana access token required: pass token= or set ASANA_ACCESS_TOKEN.")
        _check_token(resolved_token)
    stats: dict[str, int] = {}

    @dlt.resource(
        name=resource_name,
        primary_key="id",
        write_disposition="merge",
        # Rows with _deleted=True are removed from the destination on merge,
        # which propagates the deletion through cognee's orphan_cleanup.
        columns={"_deleted": {"data_type": "bool", "hard_delete": True}},
    )
    def asana_documents():
        stats.clear()
        http = service if service is not None else _make_session(resolved_token)
        try:
            client = _Client(http, check_active, resolved_token)
            yield from dlt_utils.guarded_rows(_rows(client), check_active)
        finally:
            if service is None:
                http.close()

    def _rows(client: _Client) -> Iterator[dict[str, Any]]:
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
            stats=stats,
        )

    resource = asana_documents()
    # Opt into the document ingestion path (row -> text document -> cognify).
    setattr(resource, DOCUMENT_SOURCE_ATTR, ASANA_SOURCE_NAME)
    # Give the sync state its own dlt pipeline, apart from other sources.
    setattr(resource, dlt_utils.PIPELINE_SCOPE_ATTR, resource_name)
    resource.cognee_sync_stats = stats
    return resource


def _check_resource_name(name: Any) -> None:
    """A non-empty name of at most 64 characters that dlt keeps as it is.

    It names the table and scopes the state: an empty name would lose that
    scope, and two names dlt normalizes alike ("Foo-Bar", "foo_bar") would share
    a table.
    """
    if not isinstance(name, str) or not name:
        raise ValueError("resource_name must be a non-empty string")
    if len(name) > 64:
        raise ValueError("resource_name must be at most 64 characters")
    if NamingConvention().normalize_table_identifier(name) != name:
        raise ValueError("resource_name must be lowercase snake_case (letters, digits, _)")
