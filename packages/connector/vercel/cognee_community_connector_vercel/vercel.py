"""dlt source for Vercel projects, deployments and failed-build output.

Calls, all through dlt's declarative ``rest_api`` source:

* ``GET /v10/projects``: one document per project
* ``GET /v7/deployments``: one document per deployment in the lookback window
* ``GET /v3/deployments/{id}/events``: one document per failed build, only with
  ``include_build_logs=True`` and only for deployments in state ``ERROR``

Rules the code keeps:

* Rows are rebuilt from named fields. The project object also carries env
  values, deploy-hook URLs and bypass secrets, and none of those are copied.
* Each run is a full snapshot of the window under ``replace``, and cognee forgets
  what left it.
* A failed, malformed or suspiciously empty read aborts the run, so a bad answer
  never wipes memory.
"""

import re
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from itertools import groupby
from typing import Any, NoReturn
from urllib.parse import urlsplit

import dlt
from cognee.shared.logging_utils import get_logger
from cognee.tasks.ingestion import dlt_utils
from dlt.destinations.exceptions import DatabaseUndefinedRelation
from dlt.extract.exceptions import ResourceExtractionError
from dlt.sources.helpers.requests import Client
from dlt.sources.helpers.rest_client.paginators import JSONResponseCursorPaginator
from dlt.sources.rest_api import rest_api_resources
from requests import exceptions as http_errors

logger = get_logger("vercel_connector")

# The provider tag cognee puts on every document from this source. Orphan
# cleanup matches on it, so it stays the same for every team and resource name.
VERCEL_SOURCE_NAME = "vercel"
# Table names for the default resource name, used without a team. Another
# resource name gives <resource_name>_projects and so on.
VERCEL_PROJECTS_TABLE = "vercel_projects"
VERCEL_DEPLOYMENTS_TABLE = "vercel_deployments"
VERCEL_BUILD_LOGS_TABLE = "vercel_build_logs"

# Vercel REST API host; every path below is relative to it.
_API_BASE = "https://api.vercel.com"
# Page size. The deployments list returns at most 100 a page whatever is asked
# (checked live), so asking for more would only hide that.
_PAGE_SIZE = 100
# The deployments list answers 400 when `projectIds` has more than this many items.
_MAX_PROJECT_IDS = 20
# Seconds before one request gives up. dlt's default is 60, and the wait happens
# inside cognee's staging lock, which every other dlt ingestion waits on.
_REQUEST_TIMEOUT = 30
# Longest wait before the one retry a run gets, for a 5xx or a dropped
# connection. dlt's own cap is 300 s, four times per request; one short wait
# per run rides out a blip without holding the staging lock for long.
_RETRY_WAIT = 5
# The raw lists the loaded tables are built from. They are never loaded themselves.
_RAW_PROJECTS = "vercel_raw_projects"
# The newest page of projects, read again after the full list (see _merge_head).
_RAW_PROJECTS_HEAD = "vercel_raw_projects_head"
_RAW_DEPLOYMENTS = "vercel_raw_deployments"
_RAW_FAILED = "vercel_raw_failed_deployments"
# Parent of the events call and the events themselves, never loaded as they are.
_FAILED_DEPLOYMENTS = "vercel_failed_deployments"
_BUILD_EVENTS = "vercel_build_events"
# rest_api copies parent fields onto child rows as _<parent resource>_<field>.
_PARENT_UID = f"_{_FAILED_DEPLOYMENTS}_uid"
_PARENT_NAME = f"_{_FAILED_DEPLOYMENTS}_name"
_PARENT_URL = f"_{_FAILED_DEPLOYMENTS}_inspector_url"

# A deployment id as it appears in a path, kept out of error messages.
_DEPLOYMENT_ID = re.compile(r"/dpl_[A-Za-z0-9]+")
# Prefixes of the commit fields Vercel puts in a deployment's `meta`, for example
# githubCommitSha or gitlabCommitRef, one per supported Git provider.
_GIT_PROVIDERS = ("github", "gitlab", "bitbucket")
# Put in front of a build log whose start was cut by `max_log_chars`.
_TRUNCATED_MARKER = "[earlier output truncated]"
# Lowercase words joined by single underscores. dlt folds "a__b" and a trailing
# "_" into other names, which would share tables with another resource name.
_RESOURCE_NAME = re.compile(r"[a-z][a-z0-9]*(?:_[a-z0-9]+)*")


def vercel_source(
    *,
    token: str | None = None,
    team_id: str | None = None,
    project_ids: list[str] | None = None,
    lookback_days: int | None = 30,
    include_build_logs: bool = False,
    max_log_chars: int | None = 20000,
    allow_empty: bool = False,
    resource_name: str | None = None,
    check_active: Callable[[], None] | None = None,
    service: Any = None,
    session: Any = None,
):
    """Create a dlt source that yields Vercel projects, deployments and failed-build output.

    Args:
        token: Vercel access token, sent as a bearer token. Required; the
            example reads it from ``VERCEL_TOKEN``.
        team_id: Team id (``team_...``) to read.
        project_ids: Read only these projects, by id or name (names in any case).
            Names are turned into ids through the projects list. A project named
            here that the account does not have stops the run.
        lookback_days: Read deployments created in this many days. Older ones are
            forgotten on the next sync. ``None`` reads every deployment.
        include_build_logs: Also read the build output of failed deployments. Off
            by default: a build can print a secret, and Vercel only redacts
            sensitive values of 32 characters or more.
        max_log_chars: Keep only the last N characters of a build log, and request
            only its newest ``max_log_chars // 10`` lines (at least 100). ``None``
            keeps all of it.
        allow_empty: Let one sync through that finds no projects, or only projects
            of another account, although an earlier sync found some, or that
            lacks entries of ``project_ids``. Those documents are forgotten.
            Missing ``project_ids`` entries are remembered, so later runs skip
            them with a warning and list them in ``cognee_sync_stats``, until
            they are found again.
        resource_name: Prefix of the staging tables and key of the dlt state.
            Defaults to ``vercel``, or ``vercel_<team id>`` with a team, so two
            teams in one dataset keep separate tables.
        check_active: Called before every response is used. A host raises from it
            to stop a sync whose access was revoked; the run aborts and memory
            stays as it was.
        service: ``requests.Session`` to send requests through, mainly for tests.
            By default dlt's session is used, kept short because it runs inside
            cognee's staging lock: a 429 stops the run, the first 5xx or dropped
            connection of a run gets one retry after at most 5 s and any later
            one stops it, a timeout is not retried, and each request times out
            after 30 s. ``session`` is accepted as another name for it.

    Returns:
        A dlt source for ``cognee.add(...)`` / ``cognee.remember(...)``. Its
        ``cognee_sync_stats`` holds the row counts of the last run.
    """
    # cognee marks its document-sync behaviour with this number. Version 1, in
    # 1.6.1, is the one that scopes orphan cleanup by staging table, which three
    # tables under one tag need. Later versions only add to it.
    # A dev build of cognee can carry the version but not the scope attribute
    # yet, so both are checked.
    if getattr(dlt_utils, "DOCUMENT_SYNC_VERSION", 0) < 1 or not hasattr(
        dlt_utils, "PIPELINE_SCOPE_ATTR"
    ):
        raise RuntimeError("The Vercel connector needs cognee>=1.6.1 (table-scoped cleanup).")
    if not token:
        raise ValueError("Vercel access token required: pass token=.")
    resolved_token = token
    resolved_team = team_id
    name = _resource_name(resource_name, resolved_team)
    wanted = _wanted(project_ids)
    if max_log_chars is not None and max_log_chars < 1:
        raise ValueError("max_log_chars must be a positive number of characters, or None.")
    if lookback_days is not None and lookback_days <= 0:
        # A window starting now or later lists nothing, and replace would forget it all.
        raise ValueError("lookback_days must be a positive number of days, or None.")

    tables = {kind: f"{name}_{kind}" for kind in ("projects", "deployments", "build_logs")}
    # The project filter and the window are worked out at the start of each run
    # (see _run_filters), so a reused source never sends a stale window or ids.
    run_filters: dict[str, dict] = {_RAW_DEPLOYMENTS: {}, _RAW_FAILED: {}}
    # Lines to request. If lines average at least 10 characters, this many hold
    # max_log_chars, so asking for more only moves text that is cut anyway; with
    # shorter lines the log just comes out shorter. The floor of 100 lines keeps
    # a small cap from dropping the lines around the error.
    log_lines = None if max_log_chars is None else max(100, max_log_chars // 10)
    stats: dict[str, int] = {}

    endpoints = _list_endpoints(
        team=resolved_team,
        run_filters=run_filters,
        include_build_logs=include_build_logs,
        check_active=check_active,
        stats=stats,
    )
    http = service or session or _default_session()
    client = {
        "base_url": _API_BASE,
        "auth": {"type": "bearer", "token": resolved_token},
        "session": http,
    }
    raw = {r.name: r for r in rest_api_resources({"client": client, "resources": endpoints})}

    # The projects list is read once per run, by the projects table, and handed
    # to the deployment lists through this.
    run = _RunProjects()
    loaded = [
        _table(_project_rows, tables["projects"])(
            raw[_RAW_PROJECTS],
            raw[_RAW_PROJECTS_HEAD],
            wanted,
            allow_empty,
            stats,
            run,
            getattr(http, "retry_budget", None),
            tables["projects"],
        ),
        _table(_deployment_rows, tables["deployments"], _URL_COLUMN)(
            raw[_RAW_DEPLOYMENTS],
            run,
            wanted,
            lookback_days,
            run_filters[_RAW_DEPLOYMENTS],
            stats,
            tables["deployments"],
        ),
    ]
    if include_build_logs:
        # The parent of the events call is a plain resource so that a deployment
        # the overlapping cursor read twice costs one events request, not two.
        # The failed deployments of this run, filled in by the parent and read
        # by the log table once every events call is done.
        listed: list[str] = []
        parents = dlt.resource(_failed_parents, name=_FAILED_DEPLOYMENTS)(
            raw[_RAW_FAILED],
            run,
            wanted,
            lookback_days,
            run_filters[_RAW_FAILED],
            listed,
            stats,
        )
        events_endpoint = _events_endpoint(
            team=resolved_team, check_active=check_active, log_lines=log_lines, stats=stats
        )
        # The plain resource goes last: dlt 1.9 stops reading the list after one.
        events = rest_api_resources({"client": client, "resources": [events_endpoint, parents]})
        build_events = next(r for r in events if r.name == _BUILD_EVENTS)
        loaded.append(
            _table(_build_log_documents, tables["build_logs"], _URL_COLUMN)(
                build_events, max_log_chars, log_lines, listed, stats, tables["build_logs"]
            )
        )

    # max_table_nesting=0: rows are flat already, and this keeps dlt from ever
    # unpacking a nested value into a child table that cognee would read back.
    @dlt.source(name=name, max_table_nesting=0)
    def _vercel():
        return loaded

    source = _vercel()
    # Opt into the document ingestion path (row -> text document -> cognify).
    setattr(source, dlt_utils.DOCUMENT_SOURCE_ATTR, VERCEL_SOURCE_NAME)
    # Keep this source's dlt state per dataset and per resource name.
    setattr(source, dlt_utils.PIPELINE_SCOPE_ATTR, name)
    source.cognee_sync_stats = stats
    # Parallel resources would race the projects list (see _RunProjects).
    source.parallelize = _no_parallelize
    return source


def _no_parallelize(*args: Any, **kwargs: Any) -> NoReturn:
    raise NotImplementedError(
        "Vercel: parallelize() is not supported. The deployment tables use the projects "
        "list read by the projects table in the same run, so that table has to run first."
    )


# ---------------------------------------------------------------------------
# Loaded tables
# ---------------------------------------------------------------------------


def _table(rows: Callable, name: str, columns: dict | None = None):
    """A full-snapshot table: every run replaces it, and cognee forgets what left it."""
    return dlt.resource(
        rows, name=name, primary_key="id", write_disposition="replace", columns=columns
    )


def _project_rows(
    raw,
    raw_head,
    wanted: set[str],
    allow_empty: bool,
    stats: dict,
    run: "_RunProjects",
    budget: "_RetryBudget | None",
    table: str,
):
    # The first table of a run, before any request: clear the counts, so a
    # failed run never shows the last good run's numbers as its own, and give
    # the run its one retry.
    if budget is not None:
        budget.reset()
    stats.clear()
    stats["failed_rate_limit"] = 0
    stats["failed_auth"] = 0
    stats["skipped_projects"] = []
    every = _merge_head(_unique(_items(raw, stats), "id"), list(_items(raw_head, stats)))
    run.publish(every)
    found = every
    if wanted:
        found = [project for project in every if _is_wanted_project(project, wanted)]
        _guard_missing(wanted, found, len(every), allow_empty, stats)
    _guard_empty("projects", len(found), allow_empty)
    _guard_account(found, allow_empty)
    yield [_project_row(project) for project in found]
    _finish(stats, "projects", table, len(found))


def _deployment_rows(
    raw,
    run: "_RunProjects",
    wanted: set[str],
    lookback_days: int | None,
    filters: dict,
    stats: dict,
    table: str,
):
    ids = _run_filters(filters, run.take("deployments"), wanted, lookback_days)
    if ids is not None and not ids:
        rows = []  # every selected project is gone: nothing to list
    else:
        found = _unique(_items(raw, stats), "uid")
        rows = [_deployment_row(item) for item in found if _in_scope(item, ids)]
    yield rows
    _finish(stats, "deployments", table, len(rows))


def _failed_parents(
    raw,
    run: "_RunProjects",
    wanted: set[str],
    lookback_days: int | None,
    filters: dict,
    listed: list[str],
    stats: dict,
):
    ids = _run_filters(filters, run.take("failed deployments"), wanted, lookback_days)
    parents = []
    if ids is None or ids:
        found = _unique(_items(raw, stats), "uid")
        parents = [_failed_parent(item) for item in found if _in_scope(item, ids)]
    listed[:] = [parent["uid"] for parent in parents]
    # One list: rest_api's child step iterates what it is given.
    yield parents


class _RunProjects:
    """The projects list of the current run, read once and shared.

    A second read could differ from the first: a project edited while that read
    pages moves behind the cursor and would drop out, and its deployments with
    it. The projects table is the first resource of the source, so dlt runs it
    first; each deployments list takes the list once per run, and a list that is
    missing or was already taken means the order changed, which stops the run.
    """

    def __init__(self):
        self._projects: list[dict] | None = None
        self._taken: set[str] = set()

    def publish(self, projects: list[dict]) -> None:
        self._projects = projects
        self._taken = set()

    def take(self, consumer: str) -> list[dict]:
        if self._projects is None or consumer in self._taken:
            raise RuntimeError(
                f"Vercel: the {consumer} table needs this run's projects list, and the "
                "projects table did not run before it. Loading only some tables with "
                "with_resources(), or running them with parallelize(), is not supported."
            )
        self._taken.add(consumer)
        return self._projects


def _merge_head(listed: list[dict], head: list[dict]) -> list[dict]:
    """Add the projects that were edited while the list was paged.

    The list is sorted by ``updatedAt``, newest first. A project edited during
    the paging moves to the front, behind the cursor, and would be missing from
    this run. Those projects are on the newest page, which is read again once the
    list is done; its copies are the newer ones, so they win.
    """
    merged = {project.get("id"): project for project in listed}
    merged.update((project.get("id"), project) for project in head)
    return list(merged.values())


def _build_log_documents(
    build_events,
    max_log_chars: int | None,
    log_lines: int | None,
    listed: list[str],
    stats: dict,
    table: str,
):
    """One document per failed build, keeping logs that Vercel no longer serves.

    Vercel drops build output after a while and then answers the events call with
    404 or 410. The deployment is still listed, so its log should not be
    forgotten: the row from the last load is read back from staging and loaded
    again unchanged, which keeps its document. It goes once the deployment does.
    """
    loaded = set()
    for row in _build_log_rows(_items(build_events, stats), max_log_chars, log_lines):
        loaded.add(row["id"])
        yield row
    gone = [uid for uid in listed if uid not in loaded]
    kept = list(_staged_rows(table, gone).values())
    yield kept
    if kept:
        logger.info("Vercel: kept %s build logs that Vercel no longer serves", len(kept))
    _finish(stats, "build_logs", table, len(loaded) + len(kept))


def _staged_rows(table: str, ids: list[str]) -> dict[str, dict]:
    """The rows with these ids from the last load of ``table``, if there was one."""
    if not ids:
        return {}
    pipeline = dlt.current.pipeline()
    if pipeline.destination is None:
        return {}  # iterated outside a pipeline: there is no last load
    try:
        found = _select_staged(pipeline, table)
    except DatabaseUndefinedRelation:
        # The table is missing: the first load, or logs were off until now. With
        # SELECT * no missing column can land here. Any other error aborts:
        # Vercel no longer serves these logs, so dropping them would lose them.
        logger.info("Vercel: no earlier build logs to keep in %s", table)
        return {}
    wanted = set(ids)
    return {
        row["id"]: {key: row.get(key) for key in _LOG_COLUMNS}
        for row in found
        if row.get("id") in wanted
    }


# The columns a build log row is built with, so dlt's own columns stay out.
_LOG_COLUMNS = ("id", "title", "content", "url")
# `url` is declared on the deployments and build logs tables. dlt creates no
# column that is None in every row, so without this a load where no deployment
# had an inspectorUrl has no `url`; when a later load has one, the column
# appears, every older row reads back with url NULL, and cognee, which hashes
# every column, re-ingests rows that did not change.
_URL_COLUMN = {"url": {"data_type": "text", "nullable": True}}


def _select_staged(pipeline, table: str) -> list[dict]:
    """Every row of ``table``, keyed by the column names the table really has."""
    with pipeline.sql_client() as client:
        name = client.make_qualified_table_name(table)
        with client.execute_query(f"SELECT * FROM {name}") as cursor:
            columns = list(cursor.columns_schema)
            return [dict(zip(columns, row, strict=True)) for row in cursor.fetchall()]


# ---------------------------------------------------------------------------
# Endpoint configuration
# ---------------------------------------------------------------------------


def _listing(
    path: str,
    key: str,
    team: str | None,
    check_active,
    stats: dict,
    run_params: dict | None = None,
    paginator: Any = None,
    **params: Any,
) -> dict:
    """A paged list call that aborts on a short or malformed read."""
    return {
        "path": path,
        "params": {"limit": _PAGE_SIZE, **_scope(team), **params},
        "data_selector": key,
        "paginator": paginator or _OverlappingCursor(run_params),
        "response_actions": [*_active(check_active), _rate_limited(stats), _require_list(key)],
    }


def _list_endpoints(
    *,
    team: str | None,
    run_filters: dict[str, dict],
    include_build_logs: bool,
    check_active: Callable[[], None] | None,
    stats: dict,
) -> list[dict]:
    """The raw rest_api lists the loaded tables are built from."""
    lists: dict[str, tuple] = {
        _RAW_PROJECTS: ("v10/projects", "projects", None, {}),
        _RAW_PROJECTS_HEAD: ("v10/projects", "projects", None, {"paginator": "single_page"}),
        _RAW_DEPLOYMENTS: ("v7/deployments", "deployments", run_filters[_RAW_DEPLOYMENTS], {}),
    }
    if include_build_logs:
        # The same list filtered to failed builds on the server, so successful
        # deployments never cost an events request.
        failed = run_filters[_RAW_FAILED]
        lists[_RAW_FAILED] = ("v7/deployments", "deployments", failed, {"state": "ERROR"})
    return [
        {
            "name": name,
            "selected": False,
            "endpoint": _listing(path, key, team, check_active, stats, run_params, **params),
        }
        for name, (path, key, run_params, params) in lists.items()
    ]


def _events_endpoint(
    *,
    team: str | None,
    check_active: Callable[[], None] | None,
    log_lines: int | None,
    stats: dict,
) -> dict:
    """The build output of each failed deployment."""
    if log_lines is None:
        params: dict[str, Any] = {"limit": -1, **_scope(team)}  # every line, oldest first
    else:
        # Only the newest lines; one extra tells whether earlier output exists.
        params = {"limit": log_lines + 1, "direction": "backward", **_scope(team)}
    return {
        "name": _BUILD_EVENTS,
        "include_from_parent": ["uid", "name", "inspector_url"],
        "endpoint": {
            "path": f"v3/deployments/{{resources.{_FAILED_DEPLOYMENTS}.uid}}/events",
            "params": params,
            "data_selector": "$",
            "paginator": "single_page",
            # 404 or 410: the deployment was deleted between the two calls, or
            # Vercel no longer keeps its build output. Either way nothing new can
            # be read; _build_log_documents keeps the last loaded log while the
            # deployment is listed. Any other error aborts.
            "response_actions": [
                *_active(check_active),
                _rate_limited(stats),
                {"status_code": 404, "action": "ignore"},
                {"status_code": 410, "action": "ignore"},
                _require_event_list,
            ],
        },
    }


def _scope(team: str | None) -> dict[str, str]:
    return {"teamId": team} if team else {}


def _active(check_active: Callable[[], None] | None) -> list:
    return [_still_active(check_active)] if check_active else []


def _run_filters(
    filters: dict, projects: list[dict], wanted: set[str], lookback_days: int | None
) -> set[str] | None:
    """Fill in this run's query filters for one deployments list.

    The window starts ``lookback_days`` before now, worked out per run. Selected
    projects are resolved to ids through this run's projects list, because a name sent
    to Vercel can be ambiguous: a project may be named like an id (``prj_x``),
    and ``projectIds`` given a name returns nothing. One id goes as
    ``projectId``, up to 20 as ``projectIds``, more are filtered after listing.

    Returns the selected ids to match deployments against, or None when nothing
    is selected. Deployments are matched on ``projectId`` only, never on the
    name they were built under, which an older deployment of a renamed project
    still carries.
    """
    filters.clear()
    if lookback_days is not None:
        since = datetime.now(timezone.utc) - timedelta(days=lookback_days)
        filters["since"] = int(since.timestamp() * 1000)
    if not wanted:
        return None
    ids = {
        str(project["id"])
        for project in projects
        if project.get("id") and _is_wanted_project(project, wanted)
    }
    if len(ids) == 1:
        filters["projectId"] = next(iter(ids))
    elif 1 < len(ids) <= _MAX_PROJECT_IDS:
        filters["projectIds"] = sorted(ids)
    return ids


def _in_scope(deployment: dict, ids: set[str] | None) -> bool:
    """Whether a listed deployment belongs in the snapshot."""
    return _is_live(deployment) and (ids is None or deployment.get("projectId") in ids)


class _OverlappingCursor(JSONResponseCursorPaginator):
    """Vercel's ``until`` cursor, stepped back by one millisecond.

    ``pagination.next`` is the timestamp of the page's last item and ``until`` is
    exclusive (both checked live), so a second item from that same millisecond
    would fall between two pages and be forgotten. Asking for ``next + 1`` reads
    it; the item repeated from the previous page is dropped by id afterwards. The
    cursor still has to move, or the run stops instead of looping.
    """

    def __init__(self, run_params: dict | None = None):
        super().__init__(cursor_path="pagination.next", cursor_param="until")
        self._previous: int | None = None
        # Filled in by _run_filters right before each run reads the list.
        self._run_params = run_params

    def init_request(self, request):
        self._previous = None  # a new run starts from the newest page
        if self._run_params:
            # A new dict: the one in the endpoint config is reused by every run.
            request.params = {**(request.params or {}), **self._run_params}
        super().init_request(request)

    def update_state(self, response, data=None):
        super().update_state(response, data)
        if self._next_reference is None:
            return
        cursor = int(self._next_reference) + 1
        if self._previous is not None and cursor >= self._previous:
            raise ValueError(
                f"Vercel: the page cursor from {_where(response)} did not move. Aborting "
                "so a partial snapshot cannot forget live rows."
            )
        self._previous = cursor
        self._next_reference = cursor


class _RetryBudget:
    """One retry for a whole run, used as dlt's retry condition.

    Every wait happens inside cognee's staging lock, which every other dlt
    ingestion in the process waits on, and dlt would otherwise retry each
    request on its own. With this, the first 5xx or dropped connection of a run
    is retried once after at most ``_RETRY_WAIT`` seconds, and any later one
    stops the run. A timeout is not retried, so a request that hangs costs
    ``_REQUEST_TIMEOUT`` once, and a 429 never is.
    """

    _RETRIED = (http_errors.ConnectionError, http_errors.ChunkedEncodingError)

    def __init__(self):
        self.left = 1

    def reset(self) -> None:
        self.left = 1

    def __call__(self, response, exception) -> bool:
        if isinstance(exception, http_errors.Timeout):
            return False  # ConnectTimeout is also a ConnectionError; it is not retried
        failed = isinstance(exception, self._RETRIED) or (
            response is not None and 500 <= response.status_code < 600
        )
        if not failed or self.left <= 0:
            return False
        self.left -= 1
        return True


def _default_session():
    """dlt's session, kept short because it runs inside cognee's staging lock.

    dlt's own retry rules are switched off (no status codes, no exceptions), and
    ``_RetryBudget`` decides instead: one retry per run for a 5xx or a dropped
    connection, after at most ``_RETRY_WAIT`` seconds. A timeout and a 429 are
    never retried; ``_rate_limited`` also stops the run on a 429 from inside the
    request. Each request gives up after ``_REQUEST_TIMEOUT``.
    """
    budget = _RetryBudget()
    session = Client(
        raise_for_status=False,
        status_codes=(),
        exceptions=(),
        retry_condition=budget,
        request_max_attempts=2,
        request_max_retry_delay=_RETRY_WAIT,
        request_timeout=_REQUEST_TIMEOUT,
    ).session
    session.retry_budget = budget
    return session


def _rate_limited(stats: dict):
    """Build a response hook that stops the run on a 429 instead of waiting.

    Waiting out Vercel's limit would hold cognee's staging lock, which every
    other dlt ingestion in the process waits on. The snapshot is re-read in full
    on the next run, so stopping loses nothing.
    """

    def check(response, *args, **kwargs):
        if response.status_code == 429:
            stats["failed_rate_limit"] = stats.get("failed_rate_limit", 0) + 1
            raise ValueError(
                f"Vercel: rate limited (429) on {_where(response)}. Stopping instead of "
                "waiting inside cognee's staging lock; memory is unchanged, run again later."
            )

    return check


def _resource_name(resource_name: str | None, team: str | None) -> str:
    if resource_name is not None:
        if not _RESOURCE_NAME.fullmatch(resource_name):
            raise ValueError(
                "resource_name must be lowercase words joined by single underscores, "
                f"not {resource_name!r}."
            )
        return resource_name
    if not team:
        return VERCEL_SOURCE_NAME
    name = f"{VERCEL_SOURCE_NAME}_{team.lower()}"
    if not _RESOURCE_NAME.fullmatch(name):
        raise ValueError(
            f"team_id {team!r} does not look like a team id (team_...), so it cannot "
            "name the tables. Pass the team id, or a resource_name."
        )
    return name


def _wanted(project_ids: Any) -> set[str]:
    if isinstance(project_ids, str):
        # set("prj_web") would be its letters, which match nothing.
        raise ValueError("project_ids must be a list of project ids or names, not a string.")
    return {str(item) for item in project_ids or []}


def _is_wanted_project(project: dict, wanted: set[str]) -> bool:
    # Vercel resolves a project name case-insensitively, so names match that way.
    names = {item.lower() for item in wanted}
    return project.get("id") in wanted or str(project.get("name") or "").lower() in names


def _items(resource, stats: dict) -> Any:
    """Every item of a raw rest_api resource. dlt hands them over one at a time.

    A failed request reaches this point as dlt's error, whose text carries the
    full URL, query included (team id, project ids). It is replaced by a
    ``VercelHTTPError`` that names only the status and the endpoint.
    """
    try:
        yield from resource
    except (ResourceExtractionError, http_errors.RequestException) as exc:
        mapped = _http_error(exc, stats)
        if mapped is None:
            raise
        raise mapped from None


class VercelHTTPError(RuntimeError):
    """A request to Vercel failed. The message has the status and endpoint only.

    The response, when there was one, is on ``response`` for callers that need
    more; its URL carries the query, so it is kept out of the message.
    """

    def __init__(self, status: int | None, endpoint: str, response=None, reason: str = ""):
        self.status = status
        self.endpoint = endpoint
        self.response = response
        what = f"HTTP {status}" if status else f"{reason or 'request'} failed"
        super().__init__(f"Vercel: {what} on {endpoint}. Aborting; memory is unchanged.")


class VercelAuthError(VercelHTTPError):
    """401 or 403: the token is wrong or expired, or cannot see this team."""

    def __init__(self, status: int, endpoint: str, response=None):
        super().__init__(status, endpoint, response)
        self.args = (
            f"Vercel: HTTP {status} on {endpoint}: check the token and its access to this "
            "team. Aborting; memory is unchanged.",
        )


def _http_error(exc: BaseException, stats: dict) -> VercelHTTPError | None:
    """The VercelHTTPError for a failed request under dlt's extraction errors.

    Only dlt's wrappers are looked through. Any other exception, such as the
    connector's own checks, keeps its message, and so does a body that is not
    JSON (requests counts that as a request error too).
    """
    seen: BaseException | None = exc
    while isinstance(seen, ResourceExtractionError):
        seen = seen.__cause__ or seen.__context__
    if isinstance(seen, VercelHTTPError):
        return seen
    if isinstance(seen, http_errors.HTTPError) and seen.response is not None:
        response = seen.response
        endpoint = _endpoint(response.url)
        if response.status_code in (401, 403):
            stats["failed_auth"] = stats.get("failed_auth", 0) + 1
            return VercelAuthError(response.status_code, endpoint, response)
        return VercelHTTPError(response.status_code, endpoint, response)
    if isinstance(seen, http_errors.RequestException) and not isinstance(
        seen, http_errors.InvalidJSONError
    ):
        url = seen.request.url if seen.request is not None else ""
        return VercelHTTPError(None, _endpoint(url or ""), reason=type(seen).__name__)
    return None


def _unique(items, key: str) -> list[dict]:
    """Drop the items the overlapping cursor read twice."""
    seen: set = set()
    kept = []
    for item in items:
        value = item.get(key)
        if value is None or value not in seen:
            seen.add(value)
            kept.append(item)
    return kept


def _guard_empty(kind: str, count: int, allow_empty: bool) -> None:
    """Refuse an empty snapshot when the last successful one had rows.

    A token for the wrong team, or a filter that now matches nothing, answers with
    a valid empty list, and replace would forget everything. The flag lives in the
    source's dlt state, which is only saved when a load succeeds.
    """
    state = dlt.current.source_state()
    if count == 0 and state.get(f"had_{kind}") and not allow_empty:
        raise ValueError(
            f"Vercel: no {kind} at all, although an earlier sync found some. A token for "
            "the wrong team looks like this. Aborting so memory is not wiped; pass "
            "allow_empty=True if the account really is empty."
        )
    state[f"had_{kind}"] = count > 0


def _guard_missing(
    wanted: set[str], found: list[dict], total: int, allow_empty: bool, stats: dict
) -> None:
    """Refuse a sync that lacks a project the caller listed by name or id.

    A deleted or renamed project, or a typo, would otherwise forget its documents
    without a word. ``allow_empty=True`` lets one run forget them. Each entry is
    then kept in dlt state with the time of that run, so later runs skip it
    instead of stopping, until it is found again. Skipped entries are listed in
    ``cognee_sync_stats["skipped_projects"]`` and in a warning on every run, so a
    typo accepted once stays visible.
    """
    state = dlt.current.source_state()
    missing = sorted(
        item for item in wanted if not any(_is_wanted_project(p, {item}) for p in found)
    )
    accepted = state.get("forgotten_projects") or {}
    if isinstance(accepted, list):  # written by an earlier commit, time unknown
        accepted = dict.fromkeys(accepted, "time unknown")
    new = [item for item in missing if item not in accepted]
    if new and not allow_empty:
        raise ValueError(
            f"Vercel: project_ids not found among the {total} projects this token can see: "
            f"{new}. Aborting rather than forgetting their documents. Pass allow_empty=True "
            "for one run to forget them; later runs then skip them with a warning."
        )
    now = _now()
    kept = {item: accepted.get(item, now) for item in missing}
    for item in missing:
        logger.warning(
            "Vercel: skipping project_ids entry %r: not found; an allow_empty=True run "
            "accepted it as gone (%s)",
            item,
            kept[item],
        )
    stats["skipped_projects"] = missing
    state["forgotten_projects"] = kept


def _now() -> str:
    """The time a run accepted a missing entry, as shown in its warnings."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def _guard_account(projects: list[dict], allow_empty: bool) -> None:
    """Refuse projects that all belong to another account than the last sync's.

    A token for the wrong account answers with that account's projects, and
    replace would forget every document of the right one. Each project carries
    the ``accountId`` that owns it, so the ids from the last successful sync are
    kept in the source's dlt state and compared.
    """
    state = dlt.current.source_state()
    accounts = sorted({str(p["accountId"]) for p in projects if p.get("accountId")})
    before = state.get("accounts") or []
    switched = bool(accounts and before) and not set(accounts) & set(before)
    if switched and not allow_empty:
        raise ValueError(
            "Vercel: every project belongs to another account than in the last sync. "
            "A token for the wrong account looks like this. Aborting so memory is not "
            "wiped; pass allow_empty=True to replace the old account's documents."
        )
    if switched:
        logger.warning("Vercel: allow_empty=True, replacing another account's documents")
    if accounts:
        # A list without account ids keeps the old ones, so the check holds after it.
        state["accounts"] = accounts


def _finish(stats: dict[str, int], kind: str, table: str, count: int) -> None:
    """Record and log how many rows a table got, and warn when it emptied."""
    state = dlt.current.source_state().setdefault("rows", {})
    if count == 0 and state.get(kind):
        logger.warning(
            "Vercel: %s is empty this run after %s rows last run; cognee will forget them",
            table,
            state[kind],
        )
    state[kind] = count
    stats[kind] = count
    logger.info("Vercel: %s rows in %s", count, table)


# ---------------------------------------------------------------------------
# Response guards
# ---------------------------------------------------------------------------


def _require_list(key: str):
    """Build a response hook that aborts unless the body holds a list under ``key``.

    The projects endpoint is documented with a bare-array response as well. With
    ``data_selector`` set, such a body would yield no rows and no error. A body
    without ``pagination.next`` is refused too: dlt's cursor paginator would stop
    there quietly and keep only the pages read so far.
    """

    def check(response, *args, **kwargs):
        if not response.ok:
            return  # dlt raises for error statuses once the hooks have run
        body = _json(response)
        if not isinstance(body, dict) or not isinstance(body.get(key), list):
            raise ValueError(
                f"Vercel: expected an object with a '{key}' list from {_where(response)}. "
                "Aborting so a partial snapshot cannot forget live rows."
            )
        pagination = body.get("pagination")
        if not isinstance(pagination, dict) or "next" not in pagination:
            raise ValueError(
                f"Vercel: expected a 'pagination.next' cursor from {_where(response)}. "
                "Aborting so a partial snapshot cannot forget live rows."
            )

    return check


def _require_event_list(response, *args, **kwargs):
    """Abort unless the events call returned a list of log lines."""
    if not response.ok:
        return  # 404/410 are ignored above; dlt raises for any other error status
    if not isinstance(_json(response), list):
        raise ValueError(
            f"Vercel: expected a list of build events from {_where(response)}. "
            "Aborting so a partial snapshot cannot forget live rows."
        )


def _endpoint(url: str) -> str:
    """The path of a URL, with deployment ids masked and the query left out."""
    return _DEPLOYMENT_ID.sub("/{id}", urlsplit(url).path)


def _where(response) -> str:
    """The endpoint a response came from, for the connector's own messages.

    The query (team id, project names) is left out, and so is a deployment id in
    the path, so a message names the endpoint and not the record.
    """
    return _endpoint(response.url)


def _json(response) -> Any:
    """The response body, or an abort that says the body was not JSON."""
    try:
        return response.json()
    except ValueError as exc:
        raise ValueError(
            f"Vercel: the response from {_where(response)} is not JSON. "
            "Aborting so a partial snapshot cannot forget live rows."
        ) from exc


def _still_active(check_active: Callable[[], None]):
    """Build a response hook that runs the host's ``check_active`` before a page is used."""

    def check(response, *args, **kwargs):
        check_active()

    return check


# ---------------------------------------------------------------------------
# Row builders
# ---------------------------------------------------------------------------


def _is_live(deployment: dict) -> bool:
    """False for a deleted deployment, which the list can still return.

    Vercel's spec makes ``readyState`` the required field and ``state`` optional.
    """
    return (deployment.get("state") or deployment.get("readyState")) != "DELETED"


def _project_row(project: dict) -> dict:
    """Build a project document row from named fields only.

    ``updatedAt`` is left out: it moves without any change to the fields below
    and would churn the content hash on every sync.
    """
    link = project.get("link") or {}
    repo_keys = ("org", "repo", "projectNamespace", "projectName", "owner", "slug")
    repo = "/".join(str(link[key]) for key in repo_keys if link.get(key))
    name = project.get("name") or project.get("id")
    lines = _lines(
        ("Framework", project.get("framework")),
        ("Git repository", f"{link.get('type')}: {repo}" if repo else None),
        ("Production branch", link.get("productionBranch")),
        ("Root directory", project.get("rootDirectory")),
        ("Node.js version", project.get("nodeVersion")),
        ("Created", _timestamp(project.get("createdAt"))),
    )
    return {
        "id": project.get("id"),
        "title": _clean(f"Vercel project {name}"),
        "content": "\n".join(lines),
    }


def _deployment_row(deployment: dict) -> dict:
    """Build a deployment document row from named fields only.

    The whole ``meta`` map is never copied: only the commit keys are read. The
    creator's email is left out; the username identifies the author.
    """
    meta = deployment.get("meta") or {}
    creator = deployment.get("creator") or {}
    uid = deployment.get("uid")
    name = deployment.get("name") or deployment.get("projectId") or ""
    # Vercel reports preview deployments with a null target.
    target = deployment.get("target") or "preview"
    error = ": ".join(
        str(deployment[key]) for key in ("errorCode", "errorMessage") if deployment.get(key)
    )
    host = deployment.get("url")
    lines = _lines(
        ("Project", name),
        ("Target", target),
        ("State", deployment.get("state") or deployment.get("readyState")),
        ("Created", _timestamp(deployment.get("created"))),
        ("Build started", _timestamp(deployment.get("buildingAt"))),
        # Vercel sets `ready` on failed deployments too: it is when the build ended.
        ("Finished", _timestamp(deployment.get("ready"))),
        ("Created by", creator.get("username")),
        ("Commit", _commit_meta(meta, "CommitSha")),
        ("Branch", _commit_meta(meta, "CommitRef")),
        ("Commit message", _commit_meta(meta, "CommitMessage")),
        (
            "Commit author",
            _commit_meta(meta, "CommitAuthorLogin") or _commit_meta(meta, "CommitAuthorName"),
        ),
        ("Error", error),
        ("Deployment URL", f"https://{host}" if host else None),
    )
    return {
        "id": uid,
        "title": _clean(f"Vercel deployment {uid} of {name} ({target})"),
        "content": "\n".join(lines),
        "url": _clean(deployment["inspectorUrl"]) if deployment.get("inspectorUrl") else None,
    }


def _failed_parent(deployment: dict) -> dict:
    """Reduce a failed deployment to the three fields the events call needs."""
    return {
        "uid": deployment.get("uid"),
        "name": deployment.get("name") or "",
        "inspector_url": deployment.get("inspectorUrl") or "",
    }


def _build_log_rows(events: Any, max_log_chars: int | None, line_limit: int | None = None):
    """Join the log lines of each failed deployment into one document row.

    With ``line_limit`` the events arrive newest first, at most ``line_limit + 1``
    of them: they are put back in order, and an extra line means earlier output
    was cut. A build with no log lines yields no row; its deployment document
    still carries the error.
    """
    items = events
    for uid, group in groupby(items, key=_parent_uid):
        lines = list(group)
        truncated = False
        if line_limit is not None:
            lines.reverse()
            if len(lines) > line_limit:
                lines, truncated = lines[-line_limit:], True
        text = "\n".join(filter(None, (_event_text(event) for event in lines)))
        if not uid or not text:
            continue
        if max_log_chars is not None and len(text) > max_log_chars:
            text, truncated = text[-max_log_chars:], True
        if truncated:
            text = f"{_TRUNCATED_MARKER}\n{text}"
        name = lines[0].get(_PARENT_NAME)
        yield {
            "id": uid,
            "title": _clean(f"Build output of failed Vercel deployment {uid} of {name}"),
            "content": text,
            "url": _clean(lines[0][_PARENT_URL]) if lines[0].get(_PARENT_URL) else None,
        }


def _parent_uid(event: dict) -> Any:
    if _PARENT_UID not in event:
        # rest_api names the copied parent fields. If that naming changed, every
        # log would be dropped without a word.
        raise ValueError(f"Vercel: build events without '{_PARENT_UID}'. Aborting.")
    return event[_PARENT_UID]


# Terminal escape sequences. Vercel usually strips them, but a build tool that
# forces colour can print them. In order: CSI (colour, cursor), OSC (window
# title, links) only when it is closed on the same line, a charset or similar
# selection such as ESC ( B (which `tput sgr0` writes), and two-character ones
# such as ESC M, ESC = or ESC 7. An unclosed OSC keeps its text: only the ESC
# goes, with the other control characters, so no error line is swallowed.
_ESCAPES = re.compile(
    r"\x1b\[[0-?]*[ -/]*[@-~]"
    r"|\x1b\][^\x07\x1b\n]*(?:\x07|\x1b\\)"
    r"|\x1b[ -/]+[0-~]"
    r"|\x1b[0-Z\\^-~]"
)
# Control characters other than tab and newline. A NUL would make Postgres refuse
# the whole load, so the sync would fail as long as that build is in the window.
_CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")


def _event_text(event: dict) -> str:
    """Return the log line of a build event, from either documented shape, as clean text.

    Escape sequences and control characters are removed, carriage returns become
    line breaks (real logs end progress lines with ``\\r``), and text that is not
    valid UTF-8 is replaced rather than failing the load.
    """
    text = event.get("text")
    if not isinstance(text, str):
        payload = event.get("payload")
        text = payload.get("text") if isinstance(payload, dict) else None
    if not isinstance(text, str):
        return ""
    text = _ESCAPES.sub("", text).replace("\r\n", "\n").replace("\r", "\n")
    text = _CONTROL.sub("", text).encode("utf-8", "replace").decode("utf-8")
    return text.strip("\n")


def _commit_meta(meta: dict, suffix: str) -> str | None:
    """Read one commit field from ``meta``, whichever Git provider set it."""
    for provider in _GIT_PROVIDERS:
        value = meta.get(f"{provider}{suffix}")
        if value:
            return str(value)
    return None


def _timestamp(milliseconds: Any) -> str | None:
    """Render a Vercel millisecond timestamp as UTC text, the same way every time."""
    if not isinstance(milliseconds, (int, float)) or isinstance(milliseconds, bool):
        return None
    moment = datetime.fromtimestamp(milliseconds / 1000, tz=timezone.utc)
    return moment.strftime("%Y-%m-%d %H:%M UTC")


def _lines(*pairs: tuple[str, Any]) -> list[str]:
    """Render ``label: value`` lines as clean text, skipping empty values."""
    return [_clean(f"{label}: {value}") for label, value in pairs if value not in (None, "")]


def _clean(text: str) -> str:
    """Drop control characters and replace text that is not valid UTF-8.

    A commit message or an error can carry anything, and a NUL would make
    Postgres refuse the whole load on every sync while that row is in the window.
    """
    return _CONTROL.sub("", text).encode("utf-8", "replace").decode("utf-8")
