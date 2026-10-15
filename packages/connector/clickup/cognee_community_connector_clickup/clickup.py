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
when a task points at a space the cache has not seen.

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
import os
import time
from collections.abc import Callable, Iterator
from datetime import datetime, timezone
from typing import Any

from cognee.shared.logging_utils import get_logger
from cognee.tasks.ingestion.dlt_utils import DOCUMENT_SOURCE_ATTR

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


# ---------------------------------------------------------------------------
# Auth / HTTP
# ---------------------------------------------------------------------------


def _make_session(api_token: str) -> Any:
    """Build a ``requests`` session authenticated with a ClickUp Personal API token."""
    try:
        import requests
    except ImportError as exc:
        raise ImportError(
            'The ClickUp connector requires "requests". Install with:\n'
            '    pip install "cognee-community-connector-clickup"'
        ) from exc

    session = requests.Session()
    # Personal tokens (pk_...) go in the Authorization header as-is, without "Bearer".
    session.headers.update({"Authorization": api_token.strip(), "Accept": "application/json"})
    return session


def _is_transient_network_error(exc: Exception) -> bool:
    try:
        import requests
    except ImportError:
        return False
    return isinstance(exc, requests.exceptions.Timeout | requests.exceptions.ConnectionError)


def _extract_retry_delay(headers: Any, attempt: int) -> float:
    """Wait time for a 429: ``Retry-After``, else until ``X-RateLimit-Reset``, else backoff."""
    headers = headers or {}
    retry_after = headers.get("Retry-After") or headers.get("retry-after")
    if retry_after:
        try:
            return max(1.0, float(retry_after))
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


def _request(
    session: Any,
    url: str,
    params: dict[str, Any] | None = None,
    *,
    allow_missing: bool = False,
) -> Any:
    """GET ``url`` with backoff on 429 and transient 5xx / network errors.

    Returns ``None`` for a 404 when ``allow_missing`` is set (an item deleted mid-sync).
    Auth failures raise immediately: an empty body would look like "nothing there" and
    could trigger deletions.
    """
    for attempt in range(_MAX_RETRIES):
        final_attempt = attempt == _MAX_RETRIES - 1
        try:
            response = session.request("GET", url, params=params or {}, timeout=30.0)
        except Exception as exc:
            if final_attempt or not _is_transient_network_error(exc):
                raise
            delay = float(2**attempt)
            logger.warning(
                "ClickUp: network error %s — retrying in %.1fs (%d/%d).",
                exc,
                delay,
                attempt + 1,
                _MAX_RETRIES,
            )
            time.sleep(delay)
            continue

        status = response.status_code
        if status == 429 or status in _RETRY_STATUSES:
            if final_attempt:
                response.raise_for_status()
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
            time.sleep(delay)
            continue

        if status == 401:
            raise PermissionError(
                "ClickUp rejected the API token (HTTP 401). Check CLICKUP_API_TOKEN."
            )
        if status == 404 and allow_missing:
            return None
        response.raise_for_status()
        return response.json()

    raise RuntimeError("unreachable")  # pragma: no cover


class _ClickUpAPI:
    """Thin URL helper so v2 (tasks) and v3 (docs) share one session and base URL."""

    def __init__(self, session: Any, base_url: str = DEFAULT_API_BASE) -> None:
        self.session = session
        self.base_url = base_url.rstrip("/")

    def v2(self, path: str, params: dict[str, Any] | None = None, **kwargs: Any) -> Any:
        return _request(self.session, f"{self.base_url}/v2{path}", params, **kwargs)

    def v3(self, path: str, params: dict[str, Any] | None = None, **kwargs: Any) -> Any:
        return _request(self.session, f"{self.base_url}/v3{path}", params, **kwargs)


# ---------------------------------------------------------------------------
# Workspace & hierarchy
# ---------------------------------------------------------------------------


def _resolve_team_id(api: _ClickUpAPI, configured_team_id: str | None = None) -> str:
    """Return the Workspace (team) id, picking it automatically only when unambiguous."""
    if configured_team_id:
        return str(configured_team_id)

    teams = (api.v2("/team") or {}).get("teams", [])
    if not teams:
        raise ValueError("The ClickUp token has no accessible Workspaces.")
    if len(teams) > 1:
        options = ", ".join(f"{t.get('name')!r} ({t.get('id')})" for t in teams)
        raise ValueError(f"The token can access several Workspaces; pass team_id=. {options}")
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
    ``refresh=True`` when a task references a space the cache has not seen.
    """
    if not refresh and cache.get("team_id") == team_id and "spaces" in cache:
        return cache["spaces"]

    spaces = (api.v2(f"/team/{team_id}/space", {"archived": "false"}) or {}).get("spaces", [])
    cache["team_id"] = team_id
    cache["spaces"] = {str(s["id"]): s.get("name") or str(s["id"]) for s in spaces}
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
        return ", ".join(option_label(v) for v in value)
    if kind in ("users", "people"):
        return ", ".join(_username(u) for u in value)
    if kind in ("tasks", "list_relationship"):
        return ", ".join(t.get("name") or t.get("id", "") for t in value if isinstance(t, dict))
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
        return ", ".join(_username(v) if isinstance(v, dict) else str(v) for v in value)
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
    assignees = ", ".join(_username(a) for a in task.get("assignees") or []) or "Unassigned"

    facts = [f"**Status:** {status}"]
    if priority:
        facts.append(f"**Priority:** {priority}")
    facts.append(f"**Assignees:** {assignees}")
    header = [" | ".join(facts)]

    if tags := [t.get("name") for t in task.get("tags") or [] if t.get("name")]:
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
            ("Updated", "date_updated"),
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
        for field in task.get("custom_fields") or []
        if field.get("name") and (value := _format_custom_field(field))
    ]
    if fields:
        blocks.append("## Custom Fields\n" + "\n".join(fields))

    description = (task.get("markdown_description") or task.get("text_content") or "").strip()
    if description:
        blocks.append(f"## Description\n{description}")

    checklist_blocks = []
    for checklist in task.get("checklists") or []:
        items = [
            f"- [{'x' if item.get('resolved') else ' '}] {item.get('name') or ''}"
            for item in checklist.get("items") or []
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
    for page in pages:
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
    if updated := _format_timestamp(doc.get("date_updated")):
        facts.append(f"**Updated:** {updated}")
    blocks = [" | ".join(facts)] if facts else []
    blocks.extend(_render_pages(pages))
    return "\n\n".join(blocks)


def _deleted_row(row_id: str) -> dict[str, Any]:
    return {"id": row_id, "_deleted": True}


# ---------------------------------------------------------------------------
# Tasks
# ---------------------------------------------------------------------------


def _fetch_task_comments(api: _ClickUpAPI, task_id: str) -> list[dict[str, str]]:
    """All comments on a task, oldest first.

    The endpoint returns the 25 newest comments; older ones are paged with ``start`` and
    ``start_id`` taken from the last comment of the previous page.
    """
    raw: list[dict[str, Any]] = []
    params: dict[str, Any] = {}
    while True:
        body = api.v2(f"/task/{task_id}/comment", params, allow_missing=True)
        if body is None:  # the task was deleted while we were syncing
            return []
        page = body.get("comments") or []
        fresh = [c for c in page if c.get("id") not in {r.get("id") for r in raw}]
        raw.extend(fresh)
        if len(page) < _COMMENT_PAGE_SIZE or not fresh:
            break
        params = {"start": page[-1].get("date"), "start_id": page[-1].get("id")}

    comments = []
    for comment in sorted(raw, key=lambda c: int(c.get("date") or 0)):
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
    date_updated_gt: int | None = None,
    space_ids: list[str] | None = None,
    folder_ids: list[str] | None = None,
    list_ids: list[str] | None = None,
    include_closed: bool = True,
) -> Iterator[dict[str, Any]]:
    """Yield every task in scope from the filtered team-tasks endpoint (100 per page)."""
    page = 0
    while True:
        params: dict[str, Any] = {
            "page": page,
            "subtasks": "true",
            "include_closed": "true" if include_closed else "false",
            "include_markdown_description": "true",
            "order_by": "updated",
            "reverse": "true",
        }
        if date_updated_gt:
            params["date_updated_gt"] = str(date_updated_gt)
        if space_ids:
            params["space_ids[]"] = list(space_ids)
        if folder_ids:
            params["project_ids[]"] = list(folder_ids)
        if list_ids:
            params["list_ids[]"] = list(list_ids)

        body = api.v2(f"/team/{team_id}/task", params) or {}
        tasks = body.get("tasks") or []
        yield from tasks
        if not tasks or body.get("last_page") is True or len(tasks) < _TASK_PAGE_SIZE:
            return
        page += 1


def _comments_hash(comments: list[dict[str, str]]) -> str:
    return hashlib.sha256(json.dumps(comments, sort_keys=True).encode()).hexdigest()[:16]


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
    now_ms: int | None = None,
) -> Iterator[dict[str, Any]]:
    """Yield changed tasks and deletion tombstones since the last run.

    ``state`` holds ``last_updated_ms`` (the cursor), ``known_ids``, ``scope`` and a short
    fingerprint of each task's comments; ``cache`` holds the shared hierarchy cache.
    ``state`` is written only once the whole run succeeded.

    Adding a comment bumps a task's ``date_updated`` but deleting one does not (and edits
    are not guaranteed to), so every ``comment_refresh_hours`` the comments of all tasks in
    scope are re-read and tasks whose comments changed are re-rendered. ``None`` turns
    this off.
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

    spaces = resolve_hierarchy_cache(api, team_id, cache)
    spaces_refreshed = False
    newest = cursor
    emitted: set[str] = set()
    listed: set[str] = set()

    def task_row(task: dict[str, Any], comments: list[dict[str, str]] | None) -> dict[str, Any]:
        nonlocal spaces, spaces_refreshed
        space_id = str((task.get("space") or {}).get("id") or "")
        if space_id and space_id not in spaces and not spaces_refreshed:
            spaces = resolve_hierarchy_cache(api, team_id, cache, refresh=True)
            spaces_refreshed = True
        if comments is not None:
            hashes[str(task["id"])] = _comments_hash(comments)
        emitted.add(str(task["id"]))
        return {
            "id": f"clickup:task:{task['id']}",
            "title": (task.get("name") or "").strip() or "Untitled task",
            "url": task.get("url") or None,
            "content": _render_task_markdown(task, _breadcrumb(task, spaces), comments),
            "status": (task.get("status") or {}).get("status"),
            "list": (task.get("list") or {}).get("name"),
            "date_updated": int(task.get("date_updated") or 0),
            "_deleted": False,
        }

    for task in _paginate_team_tasks(api, team_id, date_updated_gt=cursor or None, **filters):
        task_id = str(task["id"])
        listed.add(task_id)
        updated = int(task.get("date_updated") or 0)
        # ``date_updated_gt`` is inclusive in practice, so the task that set the cursor
        # comes back every run. Skip it unless something else shares that millisecond.
        if cursor and updated <= cursor and task_id in known_ids:
            continue
        newest = max(newest, updated)
        comments = _fetch_task_comments(api, task_id) if include_comments else None
        yield task_row(task, comments)

    refresh_due = (
        include_comments
        and comment_refresh_hours is not None
        and now - int(state.get("comments_refreshed_ms") or 0) >= comment_refresh_hours * 3_600_000
    )
    # A run without a cursor already listed (and rendered) everything in scope.
    everything: list[dict[str, Any]] = []
    if cursor and ((detect_deletions and known_ids) or refresh_due):
        everything = list(_paginate_team_tasks(api, team_id, **filters))
    live = listed if not cursor else {str(t["id"]) for t in everything}

    removed: set[str] = set()
    if detect_deletions and known_ids:
        if live:
            removed = known_ids - live
        else:
            logger.warning(
                "ClickUp: the task listing was empty while %d tasks are known; skipping the "
                "deletion sweep in case this is an outage.",
                len(known_ids),
            )

    rerendered = 0
    if refresh_due:
        for task in everything:
            task_id = str(task["id"])
            if task_id in emitted or task_id not in known_ids:
                continue
            comments = _fetch_task_comments(api, task_id)
            if hashes.get(task_id) == _comments_hash(comments):
                continue
            rerendered += 1
            yield task_row(task, comments)

    for task_id in sorted(removed):
        yield _deleted_row(f"clickup:task:{task_id}")

    state["scope"] = scope
    state["last_updated_ms"] = newest
    state["known_ids"] = sorted((known_ids | emitted) - removed)
    if include_comments:
        state["comment_hashes"] = {k: v for k, v in sorted(hashes.items()) if k not in removed}
    if refresh_due:
        state["comments_refreshed_ms"] = now
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


def _paginate_docs(api: _ClickUpAPI, team_id: str) -> Iterator[dict[str, Any]]:
    """Yield live (not deleted, not archived) Docs in the Workspace (v3, cursor-paged)."""
    params: dict[str, Any] = {"limit": _DOC_PAGE_SIZE}
    while True:
        body = api.v3(f"/workspaces/{team_id}/docs", params) or {}
        yield from body.get("docs") or []
        next_cursor = body.get("next_cursor")
        if not next_cursor or next_cursor == params.get("cursor"):
            return
        params = {**params, "cursor": next_cursor}


def sync_clickup_docs(
    api: _ClickUpAPI,
    state: dict[str, Any],
    cache: dict[str, Any],
    *,
    team_id: str,
    container_ids: list[str] | None = None,
    detect_deletions: bool = True,
) -> Iterator[dict[str, Any]]:
    """Yield new or edited Docs and tombstones for Docs that disappeared.

    ``state["versions"]`` maps doc id -> last ingested ``date_updated``; a doc's pages are
    fetched only when that changes. ``container_ids`` limits the sync to docs whose parent
    is one of the selected spaces, folders or lists.
    """
    versions: dict[str, Any] = dict(state.get("versions") or {})
    spaces = resolve_hierarchy_cache(api, team_id, cache)
    wanted = {str(c) for c in container_ids or []}

    live: set[str] = set()
    changed: dict[str, Any] = {}
    for doc in _paginate_docs(api, team_id):
        if doc.get("deleted") or doc.get("archived"):
            continue
        parent_id = str((doc.get("parent") or {}).get("id") or "")
        if wanted and parent_id not in wanted:
            continue
        doc_id = str(doc["id"])
        live.add(doc_id)
        if versions.get(doc_id) == doc.get("date_updated"):
            continue

        body = api.v3(
            f"/workspaces/{team_id}/docs/{doc_id}/pages",
            {"max_page_depth": -1, "content_format": "text/md"},
            allow_missing=True,
        )
        if body is None:  # deleted between listing and fetching
            live.discard(doc_id)
            continue
        pages = body if isinstance(body, list) else body.get("pages") or []
        changed[doc_id] = doc.get("date_updated")
        yield {
            "id": f"clickup:doc:{doc_id}",
            "title": (doc.get("name") or "").strip() or "Untitled doc",
            "url": f"https://app.clickup.com/{team_id}/v/dc/{doc_id}",
            "content": _render_doc_markdown(doc, pages, spaces.get(parent_id, "")),
            "date_updated": int(doc.get("date_updated") or 0),
            "_deleted": False,
        }

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
        yield _deleted_row(f"clickup:doc:{doc_id}")

    versions.update(changed)
    state["versions"] = {k: v for k, v in sorted(versions.items()) if k not in removed}
    logger.info("ClickUp: synced %d doc(s), %d deletion(s).", len(changed), len(removed))


# ---------------------------------------------------------------------------
# Public factory
# ---------------------------------------------------------------------------


def clickup_source(
    *,
    api_token: str | None = None,
    team_id: str | None = None,
    space_ids: list[str] | None = None,
    folder_ids: list[str] | None = None,
    list_ids: list[str] | None = None,
    include_comments: bool = True,
    include_closed: bool = True,
    include_docs: bool = True,
    detect_deletions: bool = True,
    comment_refresh_hours: float | None = 24.0,
    session: Any = None,
    base_url: str | None = None,
) -> Any:
    """Create a ``dlt`` source that yields ClickUp tasks and Docs as markdown documents.

    Args:
        api_token: ClickUp Personal API token (``pk_...``). Falls back to
            ``CLICKUP_API_TOKEN``.
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
        session: Pre-built HTTP session (for test injection).
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

    resolved_token = (api_token or os.environ.get("CLICKUP_API_TOKEN", "")).strip()
    if session is None and not resolved_token:
        raise ValueError(
            "ClickUp personal API token required: pass api_token= or set CLICKUP_API_TOKEN."
        )
    api = _ClickUpAPI(session or _make_session(resolved_token), base_url or DEFAULT_API_BASE)

    # Resolved on first use, inside the pipeline, so building the source makes no requests.
    resolved: dict[str, str] = {}

    def workspace() -> str:
        if "team_id" not in resolved:
            resolved["team_id"] = _resolve_team_id(api, team_id)
        return resolved["team_id"]

    def hierarchy_cache() -> dict[str, Any]:
        return dlt.current.source_state().setdefault("hierarchy", {})

    @dlt.resource(
        name=CLICKUP_TABLE_NAME,
        primary_key="id",
        write_disposition="merge",
        columns={"_deleted": {"data_type": "bool", "hard_delete": True}},
    )
    def clickup_tasks() -> Iterator[dict[str, Any]]:
        yield from sync_clickup(
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
        )

    @dlt.resource(
        name=CLICKUP_DOCS_TABLE_NAME,
        primary_key="id",
        write_disposition="merge",
        columns={"_deleted": {"data_type": "bool", "hard_delete": True}},
    )
    def clickup_docs() -> Iterator[dict[str, Any]]:
        yield from sync_clickup_docs(
            api,
            dlt.current.resource_state(),
            hierarchy_cache(),
            team_id=workspace(),
            container_ids=[*(space_ids or []), *(folder_ids or []), *(list_ids or [])],
            detect_deletions=detect_deletions,
        )

    resources: list[Callable[..., Any]] = [clickup_tasks]
    if include_docs:
        resources.append(clickup_docs)

    @dlt.source(name=CLICKUP_SOURCE_NAME)
    def _clickup():
        return resources

    source = _clickup()
    # Opt into document mode so rows route through cognify entity extraction
    setattr(source, DOCUMENT_SOURCE_ATTR, CLICKUP_SOURCE_NAME)
    return source
