"""ClickUp connector for cognee (incremental sync + forget-on-deletion).

Fetches ClickUp tasks (description, status, assignees, tags, custom fields, checklists and
comments) and Docs, and renders each into a Markdown document for cognee's memory layer
("ask my ClickUp workspace").

Document Ingestion Path (Path B):
Like the Notion connector, tasks and docs are ingested as documents rather than raw
relational tables: the source declares ``cognee_document_source = "clickup"`` (via
``DOCUMENT_SOURCE_ATTR``), so ``resolve_dlt_sources`` routes every row through standard
``cognify`` entity extraction.

Hierarchy:
ClickUp nests Workspace -> Space -> Folder -> List -> Task, and each level is a separate
call. Tasks are read workspace-wide through the filtered team-tasks endpoint, so the tree
never has to be walked to find them. Each task already names its list and folder; only
space names need a lookup, and those are cached in the source state and refreshed only
when a task points at a space the cache has not seen or when space names are updated.

Incremental sync:
Tasks are fetched with ``date_updated_gt`` set to the newest ``date_updated`` seen so far
(Unix milliseconds), kept in ``dlt.current.resource_state()``. Docs have no server-side
update filter, so the doc listing is compared with the ``date_updated`` stored per doc and
pages are fetched only for docs that changed.

Forget-on-delete:
ClickUp has no deletion feed. Each run re-lists the ids in scope and emits
``{"id": ..., "_deleted": True}`` for known tasks and docs that are gone (deleted, archived,
or moved out of scope). dlt hard-deletes those rows on merge and cognee's
``orphan_cleanup`` purges them from the graph and vector stores. An empty listing while
items are known is treated as an outage rather than a mass deletion, and state is written
only after a run succeeds.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
from collections.abc import Callable, Iterator
from datetime import datetime, timezone
from typing import Any

import requests
from cognee.shared.logging_utils import get_logger
from cognee.tasks.ingestion.dlt_utils import (
    DOCUMENT_SOURCE_ATTR,
    PIPELINE_SCOPE_ATTR,
    guarded_rows,
)

logger = get_logger("clickup_connector")

CLICKUP_SOURCE_NAME = "clickup"
CLICKUP_TABLE_NAME = "clickup_tasks"
CLICKUP_DOCS_TABLE_NAME = "clickup_docs"
DEFAULT_API_BASE = "https://api.clickup.com/api"

_TASK_PAGE_SIZE = 100  # fixed by the API
_COMMENT_PAGE_SIZE = 25  # fixed by the API
_DOC_PAGE_SIZE = 100
_MAX_RETRIES = 5
_RETRY_STATUSES = (500, 502, 503, 504)
_RESOURCE_NAME_RE = re.compile(r"^[a-z_][a-z0-9_]{0,63}\Z")


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class ClickUpError(Exception):
    """Base exception for ClickUp connector errors."""

    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        url: str | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.url = url
        self.workspaces: list[dict[str, Any]] | None = None


class ClickUpAuthenticationError(ClickUpError, PermissionError):
    """401 Unauthorized: Invalid or missing API token."""


class ClickUpPermissionError(ClickUpError, PermissionError):
    """403 Forbidden: Token lacks access to requested workspace or resource."""


class ClickUpNotFoundError(ClickUpError):
    """404 Not Found."""


class ClickUpRateLimitError(ClickUpError):
    """429 Too Many Requests."""


# ---------------------------------------------------------------------------
# Auth / HTTP
# ---------------------------------------------------------------------------


def _make_session(api_token: str) -> Any:
    """Build a ``requests`` session authenticated with a ClickUp Personal API token."""
    session = requests.Session()
    # Personal tokens (pk_...) go in the Authorization header as-is, without "Bearer".
    session.headers.update({"Authorization": api_token.strip(), "Accept": "application/json"})
    return session


def _is_transient_network_error(exc: Exception) -> bool:
    return isinstance(exc, requests.exceptions.Timeout | requests.exceptions.ConnectionError)


def _extract_retry_delay(headers: Any, attempt: int) -> float:
    """Wait time for a 429: ``Retry-After``, else until ``X-RateLimit-Reset``, else backoff."""
    headers = headers or {}
    retry_after = headers.get("Retry-After") or headers.get("retry-after")
    if retry_after:
        try:
            return min(60.0, max(1.0, float(retry_after)))
        except (TypeError, ValueError):
            pass

    reset = headers.get("X-RateLimit-Reset") or headers.get("x-ratelimit-reset")
    if reset:
        try:
            # ClickUp's limit is per minute, so a reset is never more than 60s away.
            return min(60.0, max(1.0, float(reset) - time.time()))
        except (TypeError, ValueError):
            pass

    return float(2**attempt)


def _safe_sort_number(val: Any, default: float = 0.0) -> float:
    if val is None:
        return default
    try:
        return float(val)
    except (TypeError, ValueError):
        return default


def _invoke_check_active(check_active: Callable[[], Any] | None) -> None:
    if check_active is not None:
        check_active()


def _interruptible_sleep(delay: float, check_active: Callable[[], Any] | None = None) -> None:
    if check_active is None:
        time.sleep(delay)
        return

    remaining = delay
    step = 0.5
    while remaining > 0:
        _invoke_check_active(check_active)
        current_sleep = min(step, remaining)
        time.sleep(current_sleep)
        remaining -= current_sleep
    _invoke_check_active(check_active)


def _request(
    session: Any,
    url: str,
    params: dict[str, Any] | None = None,
    *,
    allow_missing: bool = False,
    check_active: Callable[[], Any] | None = None,
    stats: dict[str, int] | None = None,
) -> Any:
    """GET ``url`` with backoff on 429 and transient 5xx / network errors.

    Returns ``None`` for a 404 when ``allow_missing`` is set (an item deleted mid-sync).
    Auth failures raise immediately: an empty body would look like "nothing there" and
    could trigger deletions.
    """
    _invoke_check_active(check_active)

    for attempt in range(_MAX_RETRIES):
        final_attempt = attempt == _MAX_RETRIES - 1
        try:
            response = session.request("GET", url, params=params or {}, timeout=30.0)
        except Exception as exc:
            if not _is_transient_network_error(exc):
                raise
            if final_attempt:
                if stats is not None:
                    stats["failed"] = stats.get("failed", 0) + 1
                raise ClickUpError(
                    f"ClickUp network error ({exc.__class__.__name__}) after retries."
                ) from None
            delay = float(2**attempt)
            logger.warning(
                "ClickUp: network error %s — retrying in %.1fs (%d/%d).",
                exc.__class__.__name__,
                delay,
                attempt + 1,
                _MAX_RETRIES,
            )
            _interruptible_sleep(delay, check_active)
            continue

        status = response.status_code
        if status == 429 or status in _RETRY_STATUSES:
            if status == 429 and stats is not None:
                stats["failed_rate_limit"] = stats.get("failed_rate_limit", 0) + 1
            if final_attempt:
                if stats is not None:
                    stats["failed"] = stats.get("failed", 0) + 1
                if status == 429:
                    raise ClickUpRateLimitError(
                        "ClickUp rate limit exceeded (HTTP 429) after retries.",
                        status_code=429,
                        url=url,
                    )
                raise ClickUpError(
                    f"ClickUp server error (HTTP {status}) after retries.",
                    status_code=status,
                    url=url,
                )
            delay = (
                _extract_retry_delay(response.headers, attempt)
                if status == 429
                else float(2**attempt)
            )
            logger.warning(
                "ClickUp: HTTP %d — retrying in %.1fs (attempt %d/%d).",
                status,
                delay,
                attempt + 1,
                _MAX_RETRIES,
            )
            _interruptible_sleep(delay, check_active)
            continue

        if status == 401:
            if stats is not None:
                stats["failed"] = stats.get("failed", 0) + 1
            raise ClickUpAuthenticationError(
                "ClickUp authentication failed (HTTP 401): invalid or expired API token.",
                status_code=401,
                url=url,
            )
        if status == 403:
            if stats is not None:
                stats["failed"] = stats.get("failed", 0) + 1
            raise ClickUpPermissionError(
                "ClickUp permission denied (HTTP 403): token lacks permission for resource.",
                status_code=403,
                url=url,
            )
        if status == 404:
            if allow_missing:
                return None
            if stats is not None:
                stats["failed"] = stats.get("failed", 0) + 1
            raise ClickUpNotFoundError(
                "ClickUp resource not found (HTTP 404).",
                status_code=404,
                url=url,
            )
        if status >= 400:
            if stats is not None:
                stats["failed"] = stats.get("failed", 0) + 1
            raise ClickUpError(
                f"ClickUp request failed with HTTP {status}.",
                status_code=status,
                url=url,
            )
        return response.json()


class _ClickUpAPI:
    """Thin URL helper so v2 (tasks) and v3 (docs) share one service and base URL."""

    def __init__(
        self,
        service: Any,
        base_url: str = DEFAULT_API_BASE,
        check_active: Callable[[], Any] | None = None,
        stats: dict[str, int] | None = None,
    ) -> None:
        self.service = service
        self.session = service  # alias for backwards compatibility
        self.base_url = base_url.rstrip("/")
        self.check_active = check_active
        self.stats = stats

    def v2(self, path: str, params: dict[str, Any] | None = None, **kwargs: Any) -> Any:
        stats = kwargs.pop("stats", self.stats)
        check_active = kwargs.pop("check_active", self.check_active)
        return _request(
            self.service,
            f"{self.base_url}/v2{path}",
            params,
            check_active=check_active,
            stats=stats,
            **kwargs,
        )

    def v3(self, path: str, params: dict[str, Any] | None = None, **kwargs: Any) -> Any:
        stats = kwargs.pop("stats", self.stats)
        check_active = kwargs.pop("check_active", self.check_active)
        return _request(
            self.service,
            f"{self.base_url}/v3{path}",
            params,
            check_active=check_active,
            stats=stats,
            **kwargs,
        )


# ---------------------------------------------------------------------------
# Workspace & hierarchy
# ---------------------------------------------------------------------------


def _resolve_team_id(api: _ClickUpAPI, configured_team_id: str | None = None) -> str:
    """Return the Workspace (team) id, picking it automatically only when unambiguous."""
    if configured_team_id:
        return str(configured_team_id)

    teams = (api.v2("/team") or {}).get("teams", [])
    if not teams:
        raise ClickUpNotFoundError("The ClickUp token has no accessible Workspaces.")
    if len(teams) > 1:
        err = ClickUpError(
            f"The token can access {len(teams)} Workspaces; pass team_id= explicitly."
        )
        err.workspaces = teams
        raise err
    return str(teams[0]["id"])


def resolve_hierarchy_cache(
    api: _ClickUpAPI,
    team_id: str,
    cache: dict[str, Any],
    *,
    refresh: bool = False,
) -> dict[str, str]:
    """Return ``{space_id: space_name}``, walking the Workspace only when needed.

    The map lives in ``cache`` (the dlt source state) so later syncs reuse it; pass
    ``refresh=True`` when a task references a space the cache has not seen or when
    space names need refreshing.
    """
    if not refresh and cache.get("team_id") == team_id and "spaces" in cache:
        return cache["spaces"]

    spaces = (api.v2(f"/team/{team_id}/space", {"archived": "false"}) or {}).get("spaces", [])
    fresh = {str(s["id"]): s.get("name") or str(s["id"]) for s in spaces}
    if cache.get("team_id") != team_id:
        cache["spaces"] = fresh
        cache["team_id"] = team_id
    else:
        cache["team_id"] = team_id
        cache.setdefault("spaces", {}).update(fresh)
    logger.info("ClickUp: cached %d space name(s).", len(cache["spaces"]))
    return cache["spaces"]


def _breadcrumb(task: dict[str, Any], spaces: dict[str, str]) -> str:
    """``Space / Folder / List`` for a task; folderless lists sit in a hidden folder."""
    space = spaces.get(str((task.get("space") or {}).get("id")), "")
    folder = task.get("folder") or task.get("project") or {}
    folder_name = "" if folder.get("hidden") else folder.get("name") or ""
    list_name = (task.get("list") or {}).get("name") or ""
    return " / ".join(part for part in (space, folder_name, list_name) if part)


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def _format_timestamp(ts_ms: Any) -> str:
    """Unix milliseconds -> ``YYYY-MM-DD HH:MM:SS UTC``."""
    if ts_ms in (None, ""):
        return ""
    try:
        moment = datetime.fromtimestamp(int(ts_ms) / 1000, tz=timezone.utc)
    except (TypeError, ValueError, OverflowError, OSError):
        return str(ts_ms)
    return moment.strftime("%Y-%m-%d %H:%M:%S UTC")


def _username(user: Any) -> str:
    if isinstance(user, dict):
        return user.get("username") or user.get("email") or str(user.get("id") or "")
    return str(user or "")


def _format_custom_field(field: dict[str, Any]) -> str:
    """Human-readable value of a task custom field, decoded by field type.

    Option-based fields return option ids (or, for older dropdowns, the option's
    ``orderindex``) rather than labels, so they are resolved through ``type_config``.
    """
    value = field.get("value")
    if value in (None, "", []):
        return ""
    kind = field.get("type")
    config = field.get("type_config") or {}
    options = config.get("options") or []

    def option_label(raw: Any) -> str:
        for option in options:
            if raw in (option.get("id"), option.get("orderindex")) or str(raw) == str(
                option.get("orderindex")
            ):
                return option.get("name") or option.get("label") or str(raw)
        return str(raw)

    if kind == "drop_down":
        return option_label(value)
    if kind == "labels":
        return ", ".join(sorted(option_label(v) for v in value))
    if kind in ("users", "people"):
        return ", ".join(sorted(_username(u) for u in value))
    if kind in ("tasks", "list_relationship"):
        return ", ".join(
            sorted(t.get("name") or str(t.get("id") or "") for t in value if isinstance(t, dict))
        )
    if kind == "date":
        return _format_timestamp(value).replace(" 00:00:00 UTC", "")
    if kind == "checkbox":
        return "Yes" if str(value).lower() == "true" else "No"
    if kind == "emoji":
        count = config.get("count")
        return f"{value}/{count}" if count else str(value)
    if kind == "currency":
        currency = config.get("currency_type") or ""
        return f"{value} {currency}".strip()
    if kind == "location" and isinstance(value, dict):
        return value.get("formatted_address") or ""
    if kind in ("manual_progress", "automatic_progress") and isinstance(value, dict):
        percent = value.get("percent_completed")
        return f"{percent}%" if percent is not None else ""
    if isinstance(value, list):
        return ", ".join(sorted(_username(v) if isinstance(v, dict) else str(v) for v in value))
    if isinstance(value, dict):
        return value.get("name") or value.get("formatted_address") or ""
    return str(value).strip()


def _render_task_markdown(
    task: dict[str, Any],
    breadcrumb: str,
    comments: list[dict[str, str]] | None = None,
) -> str:
    """Render a task into Markdown (the title is added by cognee as the heading)."""
    status = (task.get("status") or {}).get("status") or "unknown"
    priority = (task.get("priority") or {}).get("priority")
    assignee_names = sorted(_username(a) for a in task.get("assignees") or [] if _username(a))
    assignees = ", ".join(assignee_names) or "Unassigned"

    facts = [f"**Status:** {status}"]
    if priority:
        facts.append(f"**Priority:** {priority}")
    facts.append(f"**Assignees:** {assignees}")
    header = [" | ".join(facts)]

    if tags := sorted(t.get("name") for t in task.get("tags") or [] if t.get("name")):
        header.append(f"**Tags:** {', '.join(tags)}")
    if breadcrumb:
        header.append(f"**Location:** {breadcrumb}")
    if task.get("parent"):
        header.append(f"**Subtask of:** {task['parent']}")
    if creator := _username(task.get("creator")):
        header.append(f"**Creator:** {creator}")

    dates = [
        f"**{label}:** {value}"
        for label, key in (
            ("Created", "date_created"),
            ("Start", "start_date"),
            ("Due", "due_date"),
            ("Closed", "date_closed"),
        )
        if (value := _format_timestamp(task.get(key)))
    ]
    if dates:
        header.append(" | ".join(dates))
    if task.get("url"):
        header.append(f"**URL:** {task['url']}")
    blocks = ["\n".join(header)]

    fields = [
        f"- **{field['name']}:** {value}"
        for field in sorted(
            task.get("custom_fields") or [],
            key=lambda f: (str(f.get("name") or ""), str(f.get("id") or "")),
        )
        if field.get("name") and (value := _format_custom_field(field))
    ]
    if fields:
        blocks.append("## Custom Fields\n" + "\n".join(fields))

    description = (task.get("markdown_description") or task.get("text_content") or "").strip()
    if description:
        blocks.append(f"## Description\n{description}")

    checklist_blocks = []
    for checklist in sorted(
        task.get("checklists") or [],
        key=lambda c: (str(c.get("name") or ""), str(c.get("id") or "")),
    ):
        items = [
            f"- [{'x' if item.get('resolved') else ' '}] {item.get('name') or ''}"
            for item in sorted(
                checklist.get("items") or [],
                key=lambda i: (_safe_sort_number(i.get("orderindex")), str(i.get("id") or "")),
            )
        ]
        checklist_blocks.append(f"### {checklist.get('name') or 'Checklist'}\n" + "\n".join(items))
    if checklist_blocks:
        blocks.append("## Checklists\n" + "\n\n".join(checklist_blocks))

    if comments:
        lines = [f"- **{c['user']}** ({c['date']}): {c['text']}" for c in comments]
        blocks.append("## Comments\n" + "\n".join(lines))

    return "\n\n".join(blocks)


def _render_pages(pages: list[dict[str, Any]], depth: int = 2) -> list[str]:
    """Flatten nested doc pages into Markdown sections, one heading level per depth."""
    blocks: list[str] = []
    for page in sorted(
        pages, key=lambda p: (_safe_sort_number(p.get("orderindex")), str(p.get("id") or ""))
    ):
        if page.get("deleted") or page.get("archived"):
            continue
        heading = "#" * min(depth, 6)
        content = (page.get("content") or "").strip()
        blocks.append(
            f"{heading} {page.get('name') or 'Untitled page'}" + (f"\n{content}" if content else "")
        )
        blocks.extend(_render_pages(page.get("pages") or [], depth + 1))
    return blocks


def _render_doc_markdown(doc: dict[str, Any], pages: list[dict[str, Any]], location: str) -> str:
    facts = []
    if location:
        facts.append(f"**Location:** {location}")
    if created := _format_timestamp(doc.get("date_created")):
        facts.append(f"**Created:** {created}")
    blocks = [" | ".join(facts)] if facts else []
    blocks.extend(_render_pages(pages))
    return "\n\n".join(blocks)


def _deleted_row(row_id: str) -> dict[str, Any]:
    return {"id": row_id, "_deleted": True}


def _build_task_row(
    task: dict[str, Any],
    spaces: dict[str, str],
    comments: list[dict[str, str]] | None,
) -> dict[str, Any]:
    return {
        "id": f"clickup:task:{task['id']}",
        "title": (task.get("name") or "").strip() or "Untitled task",
        "url": task.get("url") or None,
        "content": _render_task_markdown(task, _breadcrumb(task, spaces), comments),
        "_deleted": False,
    }


# ---------------------------------------------------------------------------
# Tasks
# ---------------------------------------------------------------------------


def _fetch_task_comments(
    api: _ClickUpAPI,
    task_id: str,
    stats: dict[str, int] | None = None,
) -> list[dict[str, str]] | None:
    """All comments on a task, oldest first.

    The endpoint returns the 25 newest comments; older ones are paged with ``start`` and
    ``start_id`` taken from the last comment of the previous page.
    """
    raw: list[dict[str, Any]] = []
    params: dict[str, Any] = {}
    while True:
        try:
            body = api.v2(f"/task/{task_id}/comment", params, allow_missing=True, stats=None)
        except ClickUpPermissionError:
            logger.warning(
                "ClickUp: HTTP 403 reading comments for task %s; skipping comments.",
                task_id,
            )
            if stats is not None:
                stats["failed_comments"] = stats.get("failed_comments", 0) + 1
            return None
        if body is None:  # the task was deleted while we were syncing
            return []
        page = body.get("comments") or []
        fresh = [c for c in page if c.get("id") not in {r.get("id") for r in raw}]
        raw.extend(fresh)
        if len(page) < _COMMENT_PAGE_SIZE or not fresh:
            break
        params = {"start": page[-1].get("date"), "start_id": page[-1].get("id")}

    comments = []
    for comment in sorted(
        raw, key=lambda c: (_safe_sort_number(c.get("date")), str(c.get("id") or ""))
    ):
        text = (comment.get("comment_text") or "").strip()
        if text:
            comments.append(
                {
                    "user": _username(comment.get("user")) or "Unknown user",
                    "date": _format_timestamp(comment.get("date")),
                    "text": text,
                }
            )
    return comments


def _paginate_team_tasks(
    api: _ClickUpAPI,
    team_id: str,
    *,
    space_ids: list[str] | None = None,
    folder_ids: list[str] | None = None,
    list_ids: list[str] | None = None,
    date_updated_gt: int | None = None,
    include_closed: bool = True,
    seen_ids: set[str] | None = None,
    stats: dict[str, int] | None = None,
) -> tuple[list[dict[str, Any]], bool]:
    """Fetch tasks across the Workspace; returns (tasks, listing_complete).

    Pages until an empty page or last_page: True. Does not stop on short pages (< 100)
    because the API omits tasks the token cannot see.
    """
    params: dict[str, Any] = {
        "subtasks": "true",
        "include_closed": "true" if include_closed else "false",
        "include_markdown_description": "true",
    }
    if date_updated_gt is not None:
        params["date_updated_gt"] = str(date_updated_gt)
    if space_ids:
        params["space_ids[]"] = list(space_ids)
    if folder_ids:
        params["project_ids[]"] = list(folder_ids)
    if list_ids:
        params["list_ids[]"] = list(list_ids)

    all_tasks: list[dict[str, Any]] = []
    page = 0
    seen_page_fingerprints: set[str] = set()
    while True:
        params["page"] = page
        body = api.v2(f"/team/{team_id}/task", params)
        if not isinstance(body, dict) or "tasks" not in body:
            raise ClickUpError("ClickUp task listing response missing 'tasks' key.")
        tasks = body.get("tasks")
        if tasks is None or not isinstance(tasks, list):
            raise ClickUpError("ClickUp task listing response missing 'tasks' list.")
        if tasks:
            page_fp = hashlib.sha256(",".join(str(t.get("id")) for t in tasks).encode()).hexdigest()
            if page_fp in seen_page_fingerprints:
                raise ClickUpError(f"ClickUp task pagination stuck on repeated page {page}.")
            seen_page_fingerprints.add(page_fp)
            if seen_ids is not None:
                for t in tasks:
                    seen_ids.add(str(t.get("id")))
                if stats is not None:
                    stats["scanned"] = len(seen_ids)
            elif stats is not None:
                stats["scanned"] = stats.get("scanned", 0) + len(tasks)
        all_tasks.extend(tasks)
        if not tasks or body.get("last_page") is True:
            return all_tasks, True
        page += 1


def _comments_hash(comments: list[dict[str, str]]) -> str:
    return hashlib.sha256(json.dumps(comments, sort_keys=True).encode()).hexdigest()[:16]


class _HierarchyTracker:
    """Tracks and lazily refreshes space hierarchy names for tasks."""

    def __init__(self, api: _ClickUpAPI, team_id: str, cache: dict[str, Any]) -> None:
        self.api = api
        self.team_id = team_id
        self.cache = cache
        self.spaces = resolve_hierarchy_cache(api, team_id, cache)
        self.refreshed = False

    def get_spaces(self, task: dict[str, Any]) -> dict[str, str]:
        space_id = str((task.get("space") or {}).get("id") or "")
        space_name = (task.get("space") or {}).get("name")
        if space_id and space_name and self.spaces.get(space_id) != space_name:
            self.spaces[space_id] = space_name
            self.cache.setdefault("spaces", {})[space_id] = space_name
        elif space_id and space_id not in self.spaces and not self.refreshed:
            self.spaces = resolve_hierarchy_cache(self.api, self.team_id, self.cache, refresh=True)
            self.refreshed = True

        if space_id:
            container_to_space = self.cache.setdefault("container_to_space", {})
            folder_id = str((task.get("folder") or {}).get("id") or "")
            if folder_id:
                container_to_space[folder_id] = space_id
            list_id = str((task.get("list") or {}).get("id") or "")
            if list_id:
                container_to_space[list_id] = space_id

        return self.spaces

    def build_row(
        self,
        task: dict[str, Any],
        comments: list[dict[str, str]] | None,
    ) -> dict[str, Any]:
        spaces = self.get_spaces(task)
        return _build_task_row(task, spaces, comments)


def _sync_incremental_tasks(
    api: _ClickUpAPI,
    team_id: str,
    cursor: int,
    known_ids: set[str],
    include_comments: bool,
    filters: dict[str, Any],
    hierarchy: _HierarchyTracker,
    hashes: dict[str, str],
    seen_ids: set[str] | None = None,
    stats: dict[str, int] | None = None,
) -> tuple[list[dict[str, Any]], set[str], set[str], int, bool]:
    """Fetch changed tasks since cursor.

    Returns (rows, emitted_ids, listed_ids, newest_ms, listing_complete).
    """
    changed_tasks, listing_complete = _paginate_team_tasks(
        api, team_id, date_updated_gt=cursor or None, seen_ids=seen_ids, stats=stats, **filters
    )
    rows: list[dict[str, Any]] = []
    emitted: set[str] = set()
    listed: set[str] = set()
    newest = cursor

    for task in changed_tasks:
        task_id = str(task["id"])
        listed.add(task_id)
        updated = int(task.get("date_updated") or 0)
        # date_updated_gt is inclusive in practice, so the task that set the cursor
        # comes back every run. Skip it unless something else shares that millisecond.
        if cursor and updated <= cursor and task_id in known_ids:
            if stats is not None:
                stats["skipped"] = stats.get("skipped", 0) + 1
            continue
        newest = max(newest, updated)
        comments = _fetch_task_comments(api, task_id, stats) if include_comments else None
        if include_comments and comments is None:
            # 403 or unreadable: keep stored document in destination, do not overwrite with empty
            logger.warning(
                "ClickUp: cannot read comments for task %s (HTTP 403); "
                "skipping row emission to avoid wiping stored comments.",
                task_id,
            )
            continue
        if comments is not None:
            hashes[task_id] = _comments_hash(comments)
        emitted.add(task_id)
        rows.append(hierarchy.build_row(task, comments))

    return rows, emitted, listed, newest, listing_complete


def _sync_comment_refresh(
    api: _ClickUpAPI,
    state: dict[str, Any],
    candidates: list[str],
    batch_size: int,
    hierarchy: _HierarchyTracker,
    hashes: dict[str, str],
    task_map: dict[str, dict[str, Any]] | None = None,
    now: int = 0,
    stats: dict[str, int] | None = None,
) -> tuple[list[dict[str, Any]], set[str], set[str], bool]:
    """Refresh comments for a bounded batch of known unchanged tasks.

    Uses a cursor over task IDs (comment_refresh_last_id) to ensure fair rotation across
    syncs. Returns (rows, refreshed_ids, checked_ids, full_pass_completed).
    """
    if not candidates:
        return [], set(), set(), True

    last_id = state.get("comment_refresh_last_id")
    remaining = [cid for cid in candidates if cid > last_id] if last_id else candidates
    if not remaining:
        remaining = candidates
        last_id = None

    batch = remaining[:batch_size]
    full_pass_completed = len(remaining) <= batch_size
    state["comment_refresh_last_id"] = batch[-1] if not full_pass_completed else None

    task_map = task_map or {}
    rows: list[dict[str, Any]] = []
    refreshed_ids: set[str] = set()
    checked_ids: set[str] = set()
    refreshed_at = state.setdefault("comments_refreshed_at", {})

    for task_id in batch:
        comments = _fetch_task_comments(api, task_id, stats)
        if comments is None:
            # 403 or unreadable: do not advance clock for this task
            continue
        checked_ids.add(task_id)
        refreshed_at[task_id] = now
        if hashes.get(task_id) == _comments_hash(comments):
            if stats is not None:
                stats["skipped"] = stats.get("skipped", 0) + 1
            continue
        task = task_map.get(task_id) or api.v2(f"/task/{task_id}", allow_missing=True)
        if task:
            hashes[task_id] = _comments_hash(comments)
            refreshed_ids.add(task_id)
            rows.append(hierarchy.build_row(task, comments))

    return rows, refreshed_ids, checked_ids, full_pass_completed


def _sync_deletion_sweep(
    known_ids: set[str],
    live: set[str],
    full_listing_complete: bool,
    detect_deletions: bool,
) -> set[str]:
    """Identify deleted tasks across the scope, skipping if listing was incomplete."""
    if not detect_deletions or not known_ids:
        return set()

    if full_listing_complete:
        if live:
            return known_ids - live
        logger.warning(
            "ClickUp: the task listing was empty while %d tasks are known; skipping the "
            "deletion sweep in case this is an outage.",
            len(known_ids),
        )
        return set()

    logger.warning(
        "ClickUp: full task listing was incomplete; skipping deletion sweep "
        "to prevent false tombstones."
    )
    return set()


def sync_clickup(
    api: _ClickUpAPI,
    state: dict[str, Any],
    cache: dict[str, Any],
    *,
    team_id: str,
    space_ids: list[str] | None = None,
    folder_ids: list[str] | None = None,
    list_ids: list[str] | None = None,
    include_comments: bool = True,
    include_closed: bool = True,
    detect_deletions: bool = True,
    comment_refresh_hours: float | None = 24.0,
    comment_refresh_batch_size: int = 50,
    now_ms: int | None = None,
    stats: dict[str, int] | None = None,
) -> Iterator[dict[str, Any]]:
    """Yield changed tasks and deletion tombstones since the last run.

    ``state`` holds ``last_updated_ms`` (the cursor), ``known_ids``, ``scope`` and a short
    fingerprint of each task's comments; ``cache`` holds the shared hierarchy cache.
    ``state`` is written only once the whole run succeeded.
    """
    now = now_ms if now_ms is not None else int(time.time() * 1000)
    scope = _scope_key(team_id, space_ids, folder_ids, list_ids, include_closed)
    known_ids: set[str] = set(state.get("known_ids", []))
    cursor = int(state.get("last_updated_ms") or 0) if state.get("scope") == scope else 0
    hashes: dict[str, str] = dict(state.get("comment_hashes") or {})
    filters = {
        "space_ids": space_ids,
        "folder_ids": folder_ids,
        "list_ids": list_ids,
        "include_closed": include_closed,
    }

    hierarchy = _HierarchyTracker(api, team_id, cache)
    seen_task_ids: set[str] = set()

    # 1. Incremental task listing
    task_rows, emitted, listed, newest, listing_complete = _sync_incremental_tasks(
        api,
        team_id,
        cursor,
        known_ids,
        include_comments,
        filters,
        hierarchy,
        hashes,
        seen_ids=seen_task_ids,
        stats=stats,
    )
    yield from task_rows

    refresh_due = (
        include_comments
        and comment_refresh_hours is not None
        and now - int(state.get("comments_refreshed_ms") or 0) >= comment_refresh_hours * 3_600_000
    )

    # 2. Shared full listing for comment refresh and deletion sweep when cursor is active
    full_listing_complete = False
    task_map: dict[str, dict[str, Any]] = {}
    if cursor and (detect_deletions or refresh_due) and known_ids:
        all_tasks, full_listing_complete = _paginate_team_tasks(
            api, team_id, seen_ids=seen_task_ids, stats=stats, **filters
        )
        task_map = {str(t["id"]): t for t in all_tasks}
        live = set(task_map)
    else:
        live = listed
        full_listing_complete = listing_complete

    # 3. Bounded comment refresh
    rerendered = 0
    if refresh_due:
        candidates = sorted(known_ids - emitted)
        refresh_rows, refreshed_ids, checked_ids, full_pass_completed = _sync_comment_refresh(
            api,
            state,
            candidates,
            comment_refresh_batch_size,
            hierarchy,
            hashes,
            task_map,
            now=now,
            stats=stats,
        )
        yield from refresh_rows
        emitted |= refreshed_ids
        rerendered = len(refresh_rows)
        if full_pass_completed and len(checked_ids) > 0:
            state["comments_refreshed_ms"] = now

    # 4. Deletion sweep
    removed = _sync_deletion_sweep(known_ids, live, full_listing_complete, detect_deletions)
    for task_id in sorted(removed):
        if stats is not None:
            stats["deleted"] = stats.get("deleted", 0) + 1
        yield _deleted_row(f"clickup:task:{task_id}")

    # Commit state
    state["scope"] = scope
    state["last_updated_ms"] = newest
    state["known_ids"] = sorted((known_ids | emitted) - removed)
    state.setdefault("comments_refreshed_ms", now)
    if include_comments:
        state["comment_hashes"] = {k: v for k, v in sorted(hashes.items()) if k not in removed}
        refreshed_at = state.get("comments_refreshed_at") or {}
        state["comments_refreshed_at"] = {
            k: v for k, v in sorted(refreshed_at.items()) if k not in removed
        }

    logger.info(
        "ClickUp: synced %d task(s) (%d for edited/deleted comments), %d deletion(s); %d known.",
        len(emitted),
        rerendered,
        len(removed),
        len(state["known_ids"]),
    )


def _scope_key(
    team_id: str,
    space_ids: list[str] | None,
    folder_ids: list[str] | None,
    list_ids: list[str] | None,
    include_closed: bool,
) -> str:
    """Identify what is being synced; the cursor is only valid for the same selection."""
    parts = [team_id] + [
        ",".join(sorted(map(str, ids or []))) for ids in (space_ids, folder_ids, list_ids)
    ]
    return "|".join([*parts, str(include_closed)])


# ---------------------------------------------------------------------------
# Docs
# ---------------------------------------------------------------------------


def _paginate_docs(
    api: _ClickUpAPI,
    team_id: str,
    stats: dict[str, int] | None = None,
) -> Iterator[dict[str, Any]]:
    """Yield live (not deleted, not archived) Docs in the Workspace (v3, cursor-paged)."""
    params: dict[str, Any] = {"limit": _DOC_PAGE_SIZE}
    seen_doc_ids: set[str] = set()
    while True:
        body = api.v3(f"/workspaces/{team_id}/docs", params) or {}
        docs = body.get("docs") or []
        for d in docs:
            seen_doc_ids.add(str(d.get("id")))
        if stats is not None:
            stats["scanned"] = len(seen_doc_ids)
        yield from docs
        next_cursor = body.get("next_cursor")
        if not next_cursor:
            return
        if next_cursor == params.get("cursor"):
            raise ClickUpError(f"ClickUp docs pagination stuck on cursor: {next_cursor}")
        params = {**params, "cursor": next_cursor}


def sync_clickup_docs(
    api: _ClickUpAPI,
    state: dict[str, Any],
    cache: dict[str, Any],
    *,
    team_id: str,
    container_ids: list[str] | None = None,
    detect_deletions: bool = True,
    stats: dict[str, int] | None = None,
) -> Iterator[dict[str, Any]]:
    """Yield new or edited Docs and tombstones for Docs that disappeared.

    ``state["versions"]`` maps doc id -> last ingested ``date_updated`` (or page content hash
    when ``date_updated`` is missing); a doc's pages are fetched only when that changes.
    ``container_ids`` limits the sync to docs whose parent is one of the selected spaces,
    folders or lists.
    """
    versions: dict[str, Any] = dict(state.get("versions") or {})
    spaces = resolve_hierarchy_cache(api, team_id, cache)
    wanted = {str(c) for c in container_ids or []}
    container_to_space = cache.get("container_to_space", {})

    live: set[str] = set()
    changed: dict[str, Any] = {}
    skipped_unselected = 0
    for doc in _paginate_docs(api, team_id, stats=stats):
        if doc.get("deleted") or doc.get("archived"):
            if stats is not None:
                stats["filtered"] = stats.get("filtered", 0) + 1
            continue
        parent_id = str((doc.get("parent") or {}).get("id") or "")
        doc_space = container_to_space.get(parent_id)
        is_wanted = (
            (not wanted) or (parent_id in wanted) or (bool(doc_space) and doc_space in wanted)
        )
        if not is_wanted:
            skipped_unselected += 1
            if stats is not None:
                stats["filtered"] = stats.get("filtered", 0) + 1
            continue
        doc_id = str(doc["id"])
        live.add(doc_id)
        updated_ms = doc.get("date_updated")
        if updated_ms is not None and doc_id in versions and versions[doc_id] == updated_ms:
            if stats is not None:
                stats["skipped"] = stats.get("skipped", 0) + 1
            continue

        body = api.v3(
            f"/workspaces/{team_id}/docs/{doc_id}/pages",
            {"max_page_depth": -1, "content_format": "text/md"},
            allow_missing=True,
        )
        if body is None:  # deleted between listing and fetching
            live.discard(doc_id)
            if stats is not None:
                stats["skipped"] = stats.get("skipped", 0) + 1
            continue
        pages = body if isinstance(body, list) else body.get("pages") or []
        rendered_content = _render_doc_markdown(doc, pages, spaces.get(parent_id, ""))

        if updated_ms is not None:
            changed[doc_id] = updated_ms
        else:
            content_hash = hashlib.sha256(rendered_content.encode()).hexdigest()[:16]
            version_key = f"hash:{content_hash}"
            if doc_id in versions and versions[doc_id] == version_key:
                if stats is not None:
                    stats["skipped"] = stats.get("skipped", 0) + 1
                continue
            changed[doc_id] = version_key

        yield {
            "id": f"clickup:doc:{doc_id}",
            "title": (doc.get("name") or "").strip() or "Untitled doc",
            "url": f"https://app.clickup.com/{team_id}/v/dc/{doc_id}",
            "content": rendered_content,
            "_deleted": False,
        }

    if skipped_unselected:
        logger.info("ClickUp: skipped %d doc(s) outside selected containers.", skipped_unselected)

    known = set(versions)
    removed: set[str] = set()
    if detect_deletions and known:
        if live:
            removed = known - live
        else:
            logger.warning(
                "ClickUp: the doc listing was empty while %d docs are known; skipping the "
                "deletion sweep in case this is an outage.",
                len(known),
            )
    for doc_id in sorted(removed):
        if stats is not None:
            stats["deleted"] = stats.get("deleted", 0) + 1
        yield _deleted_row(f"clickup:doc:{doc_id}")

    versions.update(changed)
    state["versions"] = {k: v for k, v in sorted(versions.items()) if k not in removed}
    logger.info("ClickUp: synced %d doc(s), %d deletion(s).", len(changed), len(removed))


# ---------------------------------------------------------------------------
# Public factory
# ---------------------------------------------------------------------------


def clickup_source(
    api_token: str | None = None,
    *,
    resource_name: str | None = None,
    team_id: str | None = None,
    space_ids: list[str] | None = None,
    folder_ids: list[str] | None = None,
    list_ids: list[str] | None = None,
    include_comments: bool = True,
    include_closed: bool = True,
    include_docs: bool = True,
    detect_deletions: bool = True,
    comment_refresh_hours: float | None = 24.0,
    comment_refresh_batch_size: int = 50,
    check_active: Callable[[], Any] | None = None,
    session: Any = None,
    service: Any = None,
    base_url: str | None = None,
) -> Any:
    """Create a ``dlt`` source that yields ClickUp tasks and Docs as markdown documents.

    Args:
        api_token: ClickUp Personal API token (``pk_...``).
        resource_name: Optional prefix/name for resource and pipeline scoping.
        team_id: Workspace (team) id. Optional when the token can access exactly one.
        space_ids: Only sync tasks and docs in these Spaces.
        folder_ids: Only sync tasks and docs in these Folders.
        list_ids: Only sync tasks and docs in these Lists.
        include_comments: Fold every task comment into the task document (default True).
        include_closed: Include closed tasks (default True).
        include_docs: Also sync ClickUp Docs (default True).
        detect_deletions: Re-list ids each run to forget deleted items (default True).
        comment_refresh_hours: How often to re-read comments on unchanged tasks, since
            deleting a comment does not mark the task as updated (default 24;
            ``None`` disables it).
        comment_refresh_batch_size: Maximum tasks whose comments are refreshed per run (default 50).
        check_active: Optional callback evaluated before each request.
        session: Pre-built HTTP session (for test injection).
        service: Alias for session.
        base_url: API base URL (default ``https://api.clickup.com/api``).

    Returns:
        A ``dlt`` source configured for ``cognee.remember(...)``.
    """
    try:
        import dlt
    except ImportError as exc:
        raise ImportError(
            "The ClickUp connector requires dlt. Install with:\n"
            '    pip install "cognee-community-connector-clickup"'
        ) from exc

    if resource_name is not None and (
        not isinstance(resource_name, str) or not _RESOURCE_NAME_RE.fullmatch(resource_name)
    ):
        raise ValueError(
            f"Invalid resource_name {resource_name!r}: must start with a lowercase letter or "
            "underscore and contain only lowercase alphanumeric characters and underscores "
            "(1-64 characters)."
        )

    resolved_service = service or session
    if resolved_service is None:
        if not api_token or not isinstance(api_token, str) or not api_token.strip():
            raise ValueError("ClickUp personal API token required: pass api_token=.")
        resolved_service = _make_session(api_token.strip())

    def _make_task_stats() -> dict[str, int]:
        return {
            "scanned": 0,
            "skipped": 0,
            "filtered": 0,
            "failed": 0,
            "deleted": 0,
            "failed_rate_limit": 0,
            "failed_comments": 0,
        }

    def _make_doc_stats() -> dict[str, int]:
        return {
            "scanned": 0,
            "skipped": 0,
            "filtered": 0,
            "failed": 0,
            "deleted": 0,
            "failed_rate_limit": 0,
        }

    task_stats = _make_task_stats()
    doc_stats = _make_doc_stats()
    source_stats: dict[str, int] = {}

    api = _ClickUpAPI(
        resolved_service,
        base_url or DEFAULT_API_BASE,
        check_active=check_active,
    )

    # Resolved on first use, inside the pipeline, so building the source makes no requests.
    resolved: dict[str, str] = {}

    def workspace() -> str:
        if "team_id" not in resolved:
            resolved["team_id"] = _resolve_team_id(api, team_id)
        return resolved["team_id"]

    def hierarchy_cache() -> dict[str, Any]:
        return dlt.current.source_state().setdefault("hierarchy", {})

    tasks_table_name = f"{resource_name}_tasks" if resource_name else CLICKUP_TABLE_NAME
    docs_table_name = f"{resource_name}_docs" if resource_name else CLICKUP_DOCS_TABLE_NAME

    def _update_source_stats() -> None:
        source_stats.clear()
        source_stats.update(
            {
                "scanned": task_stats.get("scanned", 0)
                + (doc_stats.get("scanned", 0) if include_docs else 0),
                "skipped": task_stats.get("skipped", 0)
                + (doc_stats.get("skipped", 0) if include_docs else 0),
                "filtered": task_stats.get("filtered", 0)
                + (doc_stats.get("filtered", 0) if include_docs else 0),
                "failed": task_stats.get("failed", 0)
                + (doc_stats.get("failed", 0) if include_docs else 0),
                "deleted": task_stats.get("deleted", 0)
                + (doc_stats.get("deleted", 0) if include_docs else 0),
                "failed_rate_limit": task_stats.get("failed_rate_limit", 0)
                + (doc_stats.get("failed_rate_limit", 0) if include_docs else 0),
                "failed_comments": task_stats.get("failed_comments", 0),
            }
        )

    @dlt.resource(
        name=tasks_table_name,
        primary_key="id",
        write_disposition="merge",
        columns={"_deleted": {"data_type": "bool", "hard_delete": True}},
    )
    def clickup_tasks() -> Iterator[dict[str, Any]]:
        task_stats.clear()
        task_stats.update(_make_task_stats())
        api.stats = task_stats
        try:
            yield from guarded_rows(
                sync_clickup(
                    api,
                    dlt.current.resource_state(),
                    hierarchy_cache(),
                    team_id=workspace(),
                    space_ids=space_ids,
                    folder_ids=folder_ids,
                    list_ids=list_ids,
                    include_comments=include_comments,
                    include_closed=include_closed,
                    detect_deletions=detect_deletions,
                    comment_refresh_hours=comment_refresh_hours,
                    comment_refresh_batch_size=comment_refresh_batch_size,
                    stats=task_stats,
                ),
                check_active,
            )
        finally:
            _update_source_stats()

    clickup_tasks.cognee_sync_stats = task_stats

    @dlt.resource(
        name=docs_table_name,
        primary_key="id",
        write_disposition="merge",
        columns={"_deleted": {"data_type": "bool", "hard_delete": True}},
    )
    def clickup_docs() -> Iterator[dict[str, Any]]:
        doc_stats.clear()
        doc_stats.update(_make_doc_stats())
        api.stats = doc_stats
        try:
            yield from guarded_rows(
                sync_clickup_docs(
                    api,
                    dlt.current.resource_state(),
                    hierarchy_cache(),
                    team_id=workspace(),
                    container_ids=[*(space_ids or []), *(folder_ids or []), *(list_ids or [])],
                    detect_deletions=detect_deletions,
                    stats=doc_stats,
                ),
                check_active,
            )
        finally:
            _update_source_stats()

    clickup_docs.cognee_sync_stats = doc_stats

    resources: list[Callable[..., Any]] = [clickup_tasks]
    if include_docs:
        resources.append(clickup_docs)

    @dlt.source(name=resource_name or CLICKUP_SOURCE_NAME)
    def _clickup():
        return resources

    source = _clickup()
    # Attach stats references directly to the DltResource objects and source
    source.resources[tasks_table_name].cognee_sync_stats = task_stats
    if include_docs:
        source.resources[docs_table_name].cognee_sync_stats = doc_stats
    source.cognee_sync_stats = source_stats

    # Opt into document mode so rows route through cognify entity extraction
    setattr(source, DOCUMENT_SOURCE_ATTR, CLICKUP_SOURCE_NAME)
    setattr(source, PIPELINE_SCOPE_ATTR, resource_name or CLICKUP_SOURCE_NAME)
    _update_source_stats()
    return source
