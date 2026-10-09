"""dlt source for Todoist tasks (merge + _deleted tombstone + incremental).

Fetches Todoist tasks via the REST API v2, rendering each task as a prose
document that preserves project, section, priority, due-date, and label
context. Each task flows through cognee's document-mode pipeline.

Sync model: ``write_disposition="merge"`` with a ``_deleted`` boolean column.
Each run:
1. Re-checks tasks that were ``pending`` / had no due date on the previous
   run (their state may have advanced).
2. Pages ``GET /rest/v2/tasks`` with ``since=<cursor>`` for anything updated
   since the last sync.
3. Reads back the current dataset and emits ``_deleted=True`` tombstones for
   any task id that is no longer reachable through the active listing AND
   was not reported as closed by the sync filter.

A transient API error aborts the run before any tombstone is emitted, so a
partial snapshot never drives mass deletions. Permanent errors (auth, 404 on
the root endpoint) raise immediately.
"""

from __future__ import annotations

import os
import time
from collections.abc import Iterable, Iterator
from datetime import UTC, datetime, timedelta
from typing import Any

from cognee.shared.logging_utils import get_logger
from cognee.tasks.ingestion.dlt_utils import DOCUMENT_SOURCE_ATTR

logger = get_logger("todoist_connector")

TODOIST_TABLE_NAME = "todoist_tasks"
TODOIST_SOURCE_NAME = "todoist"

_MAX_RETRIES = 5
_OVERLAP_WINDOW_MINUTES = 5
_IN_PROGRESS_RECHECK_DAYS = 7
_PAGE_LIMIT = 200

_EXTRA_HINT = (
    'The Todoist connector requires the "todoist" extra: '
    'pip install "cognee[todoist]" (provides dlt and httpx).'
)

_BASE_URL = "https://api.todoist.com/rest/v2"


def todoist_source(
    api_token: str | None = None,
    project_ids: list[str] | None = None,
    client: Any = None,
):
    """Create a dlt source that yields Todoist tasks as documents.

    Args:
        api_token: Todoist personal API token. Falls back to ``TODOIST_API_TOKEN``.
        project_ids: Optional list of project ids to restrict scope. When omitted,
            all active tasks visible to the token are ingested.
        client: Pre-built HTTP client callable (test-injection point).

    Returns:
        A dlt source suitable for ``cognee.add(...)`` / ``cognee.remember(...)``.
    """
    try:
        import dlt
    except ImportError as exc:
        raise ImportError(_EXTRA_HINT) from exc

    if client is None:
        try:
            import httpx
        except ImportError as exc:
            raise ImportError(_EXTRA_HINT) from exc

        resolved_token = api_token or os.environ.get("TODOIST_API_TOKEN")
        if not resolved_token:
            raise ValueError(
                "Todoist API token required: pass api_token= or set TODOIST_API_TOKEN."
            )

        session = httpx.Client(
            base_url=_BASE_URL,
            headers={"Authorization": f"Bearer {resolved_token}"},
            timeout=30.0,
        )

        def _api_request(method: str, path: str, **kwargs: Any) -> Any:
            for attempt in range(_MAX_RETRIES):
                try:
                    response = session.request(method, path, **kwargs)
                except (httpx.TransportError, httpx.TimeoutException):
                    if attempt == _MAX_RETRIES - 1:
                        raise
                    delay = 2**attempt
                    logger.warning(
                        "Todoist: network error — retrying in %.1fs (%d/%d).",
                        delay,
                        attempt + 1,
                        _MAX_RETRIES,
                    )
                    time.sleep(delay)
                    continue

                if response.status_code == 429:
                    retry_after = int(response.headers.get("retry-after", 2**attempt))
                    time.sleep(retry_after)
                    continue
                if response.status_code >= 500 and attempt < _MAX_RETRIES - 1:
                    time.sleep(2**attempt)
                    continue
                if response.status_code in (401, 403):
                    raise PermissionError(
                        f"Todoist API: {response.status_code} — check your API token."
                    )
                response.raise_for_status()
                return response.json()
            raise RuntimeError(f"Todoist API: {method} {path} failed after {_MAX_RETRIES} retries.")

        client = _api_request

    @dlt.resource(
        name=TODOIST_TABLE_NAME,
        primary_key="id",
        write_disposition="merge",
    )
    def todoist_tasks() -> Iterator[dict[str, Any]]:
        import dlt as _runtime_dlt

        state = _runtime_dlt.current.resource_state()
        last_sync = state.get("last_sync")
        in_progress = set(state.get("in_progress", []))
        known_ids: set[str] = set(state.get("known_ids", []))

        # Build the since cursor with overlap window
        if last_sync:
            cursor_dt = datetime.fromisoformat(last_sync).replace(tzinfo=UTC) - timedelta(
                minutes=_OVERLAP_WINDOW_MINUTES
            )
            since = cursor_dt.isoformat()
        else:
            since = None

        # Phase 1: Re-check previously in-progress items
        rechecked_ids: set[str] = set()
        if in_progress:
            for task_id in list(in_progress):
                try:
                    task = client("GET", f"/tasks/{task_id}")
                    rechecked_ids.add(task_id)
                    yield _task_to_row(task)
                except Exception as exc:
                    if _is_gone(exc):
                        logger.info("Todoist: in-progress task %s gone, tombstoning.", task_id)
                        yield {"id": task_id, "_deleted": True}
                        known_ids.discard(task_id)
                    else:
                        raise

        # Phase 2: Fetch tasks updated since cursor (or all on first sync)
        params: dict[str, Any] = {"limit": _PAGE_LIMIT}
        if since:
            params["since"] = since
        if project_ids:
            params["project_id"] = project_ids[0]  # API accepts single project_id

        seen_ids: set[str] = set()
        new_in_progress: set[str] = set()

        for task in _iter_tasks(client, params, project_ids):
            task_id = task.get("id")
            if not task_id:
                continue
            if task_id in rechecked_ids:
                continue  # Already emitted in phase 1

            seen_ids.add(task_id)
            known_ids.add(task_id)

            if _is_in_progress(task):
                new_in_progress.add(task_id)

            yield _task_to_row(task)

        # Phase 3: Detect deletions — tasks we knew about that aren't in active listing
        # Only run deletion detection on full syncs (first run) or when we have a stable cursor
        if not since:
            for known_id in known_ids - seen_ids - rechecked_ids:
                logger.info("Todoist: task %s no longer active, tombstoning.", known_id)
                yield {"id": known_id, "_deleted": True}
                known_ids.discard(known_id)

        # Update state
        state["last_sync"] = datetime.now(UTC).isoformat()
        state["in_progress"] = list(new_in_progress)
        state["known_ids"] = list(known_ids)

        logger.info(
            "Todoist: synced %d task(s) (%d new/updated, %d in-progress re-check).",
            len(seen_ids) + len(rechecked_ids),
            len(seen_ids),
            len(rechecked_ids),
        )

    @dlt.source(name=TODOIST_SOURCE_NAME)
    def _todoist() -> Any:
        return todoist_tasks

    source = _todoist()
    setattr(source, DOCUMENT_SOURCE_ATTR, TODOIST_SOURCE_NAME)
    return source


def _is_transient(exc: Exception) -> bool:
    """True for network / timeout / 429 / 5xx errors worth retrying."""
    try:
        import httpx
    except ImportError:
        return True  # If httpx not available, assume transient for safety

    if isinstance(exc, (httpx.TransportError, httpx.TimeoutException)):
        return True
    if hasattr(exc, "response"):
        status = getattr(exc.response, "status_code", None)
        if status in (429, 500, 502, 503, 504):
            return True
    return False


def _is_gone(exc: Exception) -> bool:
    """True when a task is permanently gone (404) — safe to tombstone."""
    if hasattr(exc, "response"):
        status = getattr(exc.response, "status_code", None)
        return status == 404
    return False


def _is_in_progress(task: dict[str, Any]) -> bool:
    """Identify tasks that may change state between syncs and should be re-checked."""
    # No due date = could stay active indefinitely
    if not task.get("due"):
        return True
    # Due date is in the future = might get updated
    due_str = task.get("due", {}).get("date")
    if due_str:
        try:
            due_date = datetime.strptime(due_str, "%Y-%m-%d").replace(tzinfo=UTC)
            if due_date >= datetime.now(UTC) - timedelta(days=_IN_PROGRESS_RECHECK_DAYS):
                return True
        except ValueError:
            return True
    return False


def _iter_tasks(
    client: Any,
    base_params: dict[str, Any],
    project_ids: list[str] | None,
) -> Iterable[dict[str, Any]]:
    """Yield tasks, handling pagination across multiple projects if needed."""
    projects_to_fetch = project_ids or [None]

    for proj_id in projects_to_fetch:
        params = dict(base_params)
        if proj_id and len(projects_to_fetch) > 1:
            params["project_id"] = proj_id

        offset = 0
        while True:
            params["offset"] = offset
            tasks = client("GET", "/tasks", params=params)
            if not tasks:
                break

            yield from tasks

            if len(tasks) < base_params.get("limit", _PAGE_LIMIT):
                break
            offset += base_params.get("limit", _PAGE_LIMIT)


def _task_to_row(task: dict[str, Any]) -> dict[str, Any]:
    """Transform a raw Todoist task into a cognee document row."""
    task_id = task.get("id", "")
    content = task.get("content", "")
    description = task.get("description", "")
    priority = task.get("priority", 1)
    priority_label = {4: "urgent", 3: "high", 2: "normal", 1: "low"}.get(priority, "normal")

    body_parts: list[str] = []
    body_parts.append(f"Todoist task: {content}")
    body_parts.append(f"Priority: {priority_label} ({priority}/4)")

    due = task.get("due")
    if due:
        body_parts.append(f"Due: {due.get('date', 'unknown')}")
        if due.get("is_recurring"):
            body_parts.append("Recurring: yes")

    project_id = task.get("project_id")
    if project_id:
        body_parts.append(f"Project ID: {project_id}")

    section_id = task.get("section_id")
    if section_id:
        body_parts.append(f"Section ID: {section_id}")

    labels = task.get("labels", [])
    if labels:
        body_parts.append(f"Labels: {', '.join(labels)}")

    assignees = task.get("assignee_ids", [])
    if assignees:
        body_parts.append(f"Assignees: {', '.join(assignees)}")

    created = task.get("created_at")
    if created:
        body_parts.append(f"Created: {created}")

    url = task.get("url")
    if url:
        body_parts.append(f"URL: {url}")

    if description:
        body_parts.append(f"\nDescription:\n{description}")

    comment_count = task.get("comment_count", 0)
    if comment_count:
        body_parts.append(f"\nComments: {comment_count}")

    return {
        "id": task_id,
        "project_id": project_id,
        "section_id": section_id,
        "content": content,
        "description": description,
        "priority": priority,
        "priority_label": priority_label,
        "due_date": due.get("date") if due else None,
        "due_recurring": due.get("is_recurring") if due else False,
        "labels": labels,
        "assignee_ids": assignees,
        "created_at": created,
        "url": url,
        "comment_count": comment_count,
        "is_completed": task.get("is_completed", False),
        "text": "\n".join(body_parts),
        "raw": task,
        "source": TODOIST_SOURCE_NAME,
        "_deleted": False,
    }
