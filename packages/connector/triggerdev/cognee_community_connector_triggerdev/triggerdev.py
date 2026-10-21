"""Trigger.dev connector for cognee: a dlt resource that syncs background tasks, workflow runs, and schedules into memory.

It ingests your Trigger.dev project's task definitions, run executions (including error stacktraces),
and cron schedules, and is meant to be handed to ``cognee.remember``::

    await cognee.remember(
        triggerdev_source(api_key="<triggerdev api key>", resource_name="triggerdev_prod"),
        dataset_name="ops_memory",
        primary_key="id",
        write_disposition="merge",  # required for incremental upsert
    )
"""

import hashlib
import json
import re
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from typing import Any

import requests
from cognee.shared.logging_utils import get_logger
from cognee.tasks.ingestion import dlt_utils
from cognee.tasks.ingestion.dlt_utils import DOCUMENT_SOURCE_ATTR

logger = get_logger("triggerdev_connector")

DEFAULT_API_URL = "https://api.trigger.dev"
DEFAULT_DASHBOARD_URL = "https://cloud.trigger.dev"

KINDS = ("tasks", "runs", "schedules")
PAGE_SIZE = 100
DEFAULT_MAX_REQUESTS = 500
_TIMEOUT = (10, 30)
_RETRIES = 3

# Regex to scrub sensitive tokens and secrets from logs and error details
_SECRET_PATTERNS = [
    re.compile(r"(tr_(?:prod|dev)_[A-Za-z0-9_\-]{8,})"),
    re.compile(r"(Bearer\s+[A-Za-z0-9_\-\.]{8,})", re.IGNORECASE),
    re.compile(r"(?:api[_-]?key|secret|password|token)\s*[:=]\s*['\"]?([A-Za-z0-9_\-\.]{6,})['\"]?", re.IGNORECASE),
]


def sanitize_text(text: str | None) -> str:
    """Sanitize sensitive secrets, keys, or credentials from raw text."""
    if not text:
        return ""
    result = text
    for pattern in _SECRET_PATTERNS:
        result = pattern.sub("[REDACTED]", result)
    return result


class TriggerDevSourceError(RuntimeError):
    """Base exception for Trigger.dev source errors."""


class TriggerDevAPIError(TriggerDevSourceError):
    """API communication failure or unexpected error."""


class TriggerDevAuthError(TriggerDevSourceError):
    """Trigger.dev rejected the API key (HTTP 401)."""


class TriggerDevAccessError(TriggerDevSourceError):
    """The API key does not have permission for the requested resource (HTTP 403)."""


class TriggerDevRateLimitedError(TriggerDevSourceError):
    """Rate limit exceeded (HTTP 429)."""


class TriggerDevNotFoundError(TriggerDevAPIError):
    """Requested resource was not found (HTTP 404)."""


class _CutShortError(Exception):
    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


class TriggerDevClient:
    """HTTP client for the Trigger.dev REST API v3."""

    def __init__(
        self,
        api_key: str,
        api_url: str = DEFAULT_API_URL,
        dashboard_url: str = DEFAULT_DASHBOARD_URL,
        sleep: Callable[[float], None] = time.sleep,
    ):
        key = (api_key or "").strip()
        if not key:
            raise ValueError("The Trigger.dev API key is empty or has an invalid format")
        self._api_key = key
        self.api_url = api_url.rstrip("/")
        self.dashboard_url = dashboard_url.rstrip("/")
        self._sleep = sleep
        self.rate_limit: dict[str, int | None] = {}

    def __repr__(self) -> str:
        return f"TriggerDevClient(api_url='{self.api_url}', api_key=<redacted>)"

    def request(
        self,
        method: str,
        path: str,
        *,
        body: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
    ) -> Any:
        url = f"{self.api_url}{path}" if path.startswith("/") else f"{self.api_url}/{path}"
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        for attempt in range(_RETRIES):
            last = attempt == _RETRIES - 1
            try:
                response = requests.request(
                    method,
                    url,
                    json=body,
                    params=params,
                    headers=headers,
                    timeout=_TIMEOUT,
                )
            except requests.RequestException as exc:
                if last:
                    raise TriggerDevAPIError(f"Trigger.dev request failed: {type(exc).__name__}") from None
                self._sleep(2**attempt)
                continue

            if response.status_code >= 500 and not last:
                self._sleep(2**attempt)
                continue

            return self._parse(response, path)

    def _parse(self, response: requests.Response, path: str) -> Any:
        status = response.status_code
        if status == 401:
            raise TriggerDevAuthError("Trigger.dev rejected the API key (HTTP 401)")
        if status == 403:
            raise TriggerDevAccessError(f"Trigger.dev access forbidden for {path} (HTTP 403)")
        if status == 429:
            retry_after = response.headers.get("Retry-After")
            msg = f"Trigger.dev rate limit exceeded (HTTP 429), retry after {retry_after}s" if retry_after else "Trigger.dev rate limit exceeded (HTTP 429)"
            raise TriggerDevRateLimitedError(msg)
        if status == 404:
            raise TriggerDevNotFoundError(f"Trigger.dev resource not found (HTTP 404): {path}")

        try:
            payload = response.json()
        except ValueError:
            payload = None

        if status not in (200, 201):
            raise TriggerDevAPIError(f"Trigger.dev API request failed with HTTP {status}: {response.text[:200]}")

        return payload


def render_task(task: dict[str, Any], dashboard_url: str = DEFAULT_DASHBOARD_URL) -> dict[str, Any]:
    """Render a task definition into a deterministic document."""
    task_id = str(task.get("id") or task.get("slug") or "")
    slug = task.get("slug") or task.get("id") or "unnamed_task"
    title = f"Task: {slug}"

    fields = [
        f"Task Slug: {slug}",
        f"Task ID: {task_id}",
        f"File Path: {task.get('filePath') or 'N/A'}",
        f"Export Name: {task.get('exportName') or 'N/A'}",
        f"Trigger Source: {task.get('triggerSource') or 'code'}",
        f"Created At: {task.get('createdAt') or 'N/A'}",
        f"Updated At: {task.get('updatedAt') or 'N/A'}",
    ]

    content = sanitize_text("\n".join(fields))
    return {
        "id": f"task:{task_id}",
        "title": title,
        "content": content,
        "url": f"{dashboard_url}/tasks/{task_id}",
        "_deleted": False,
    }


def render_run(run: dict[str, Any], dashboard_url: str = DEFAULT_DASHBOARD_URL) -> dict[str, Any]:
    """Render an execution run into a deterministic document with error & timing details."""
    run_id = str(run.get("id") or "")
    task_slug = run.get("taskIdentifier") or run.get("taskSlug") or "unknown_task"
    number = run.get("number") or run_id[:8]
    status = run.get("status") or "UNKNOWN"
    title = f"Task Run: {task_slug} (#{number}) - {status}"

    fields = [
        f"Task: {task_slug}",
        f"Run ID: {run_id}",
        f"Status: {status}",
        f"Run Number: {number}",
        f"Started At: {run.get('startedAt') or run.get('createdAt') or 'N/A'}",
        f"Finished At: {run.get('finishedAt') or 'N/A'}",
        f"Duration: {run.get('durationMs') or 0}ms",
        f"Is Test Run: {run.get('isTest', False)}",
    ]

    tags = run.get("tags") or []
    if tags:
        fields.append(f"Tags: {', '.join(str(t) for t in tags)}")

    error = run.get("error")
    if error and isinstance(error, dict):
        err_name = error.get("name") or "Error"
        err_msg = error.get("message") or "Unknown error"
        fields.append(f"Error Name: {err_name}")
        fields.append(f"Error Message: {err_msg}")
        stack = error.get("stack")
        if stack:
            fields.append(f"Stacktrace:\n{stack}")
    elif error and isinstance(error, str):
        fields.append(f"Error: {error}")

    output = run.get("output")
    if output:
        output_str = json.dumps(output) if isinstance(output, (dict, list)) else str(output)
        if len(output_str) > 1000:
            output_str = output_str[:1000] + "... [truncated]"
        fields.append(f"Output Preview: {output_str}")

    content = sanitize_text("\n".join(fields))
    return {
        "id": f"run:{run_id}",
        "title": title,
        "content": content,
        "url": f"{dashboard_url}/runs/{run_id}",
        "_deleted": False,
    }


def render_schedule(schedule: dict[str, Any], dashboard_url: str = DEFAULT_DASHBOARD_URL) -> dict[str, Any]:
    """Render a cron schedule into a deterministic document."""
    sched_id = str(schedule.get("id") or "")
    task_slug = schedule.get("taskIdentifier") or "unknown_task"
    cron = schedule.get("cron") or schedule.get("cronExpression") or "N/A"
    active = schedule.get("active", True)
    title = f"Schedule: {task_slug} ({cron})"

    fields = [
        f"Schedule ID: {sched_id}",
        f"Task: {task_slug}",
        f"Cron Expression: {cron}",
        f"Timezone: {schedule.get('timezone') or 'UTC'}",
        f"Status: {'Active' if active else 'Paused'}",
        f"Last Run Timestamp: {schedule.get('lastRunTimestamp') or 'N/A'}",
        f"Next Run Timestamp: {schedule.get('nextRunTimestamp') or 'N/A'}",
    ]

    content = sanitize_text("\n".join(fields))
    return {
        "id": f"schedule:{sched_id}",
        "title": title,
        "content": content,
        "url": f"{dashboard_url}/schedules/{sched_id}",
        "_deleted": False,
    }


def _hash(val: Any) -> str:
    """Compute deterministic SHA-256 fingerprint for document content."""
    serialized = json.dumps(val, sort_keys=True, default=str)
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()[:16]


class _Walker:
    def __init__(
        self,
        client: TriggerDevClient,
        state: dict[str, Any],
        stats: dict[str, int],
        *,
        include: tuple[str, ...],
        max_requests: int = DEFAULT_MAX_REQUESTS,
        check_active: Callable[[], None] | None = None,
    ):
        self.client = client
        self.state = state
        self.stats = stats
        self.stats.setdefault("scanned", 0)
        self.stats.setdefault("skipped", 0)
        self.stats.setdefault("deleted", 0)
        self.stats.setdefault("failed", 0)
        self.include = include
        self.max_requests = max_requests
        self.check_active = check_active
        self.requests_used = 0

        self.fingerprints: dict[str, str] = self.state.setdefault("fingerprints", {})
        self.seen: list[str] = []
        self.emitted = 0

    def _call(self, method: str, path: str, **kwargs) -> Any:
        if self.check_active is not None:
            self.check_active()
        if self.requests_used >= self.max_requests:
            raise _CutShortError("budget")
        self.requests_used += 1
        return self.client.request(method, path, **kwargs)

    def _changed(self, row_id: str, fingerprint: str) -> bool:
        self.seen.append(row_id)
        self.stats["scanned"] += 1
        if self.fingerprints.get(row_id) == fingerprint:
            self.stats["skipped"] += 1
            return False
        return True

    def _emit(self, row: dict[str, Any], fingerprint: str) -> dict[str, Any]:
        self.fingerprints[row["id"]] = fingerprint
        self.emitted += 1
        return row

    def rows(self) -> Iterator[dict[str, Any]]:
        # Process any pending tombstones from previous interrupted runs
        yield from self._forget()

        if "tasks" in self.include:
            yield from self._task_rows()
        if "schedules" in self.include:
            yield from self._schedule_rows()
        if "runs" in self.include:
            yield from self._run_rows()

        self._close_cycle()
        yield from self._forget()

    def _task_rows(self) -> Iterator[dict[str, Any]]:
        try:
            data = self._call("GET", "/api/v1/tasks")
            tasks = data if isinstance(data, list) else (data.get("tasks") or data.get("data") or [])
        except TriggerDevNotFoundError:
            tasks = []
        except Exception as exc:
            logger.warning("Failed to list Trigger.dev tasks: %s", exc)
            tasks = []

        for task in tasks:
            row = render_task(task, getattr(self.client, "dashboard_url", DEFAULT_DASHBOARD_URL))
            fingerprint = _hash(row)
            if self._changed(row["id"], fingerprint):
                yield self._emit(row, fingerprint)

    def _schedule_rows(self) -> Iterator[dict[str, Any]]:
        try:
            data = self._call("GET", "/api/v3/schedules")
            schedules = data if isinstance(data, list) else (data.get("schedules") or data.get("data") or [])
        except TriggerDevNotFoundError:
            schedules = []
        except Exception as exc:
            logger.warning("Failed to list Trigger.dev schedules: %s", exc)
            schedules = []

        for schedule in schedules:
            row = render_schedule(schedule, getattr(self.client, "dashboard_url", DEFAULT_DASHBOARD_URL))
            fingerprint = _hash(row)
            if self._changed(row["id"], fingerprint):
                yield self._emit(row, fingerprint)

    def _run_rows(self) -> Iterator[dict[str, Any]]:
        cursor = self.state.get("runs_cursor")
        params: dict[str, Any] = {"limit": PAGE_SIZE}
        if cursor:
            params["cursor"] = cursor

        while True:
            data = self._call("GET", "/api/v3/runs", params=params)
            runs = data if isinstance(data, list) else (data.get("runs") or data.get("data") or [])
            pagination = data.get("pagination") if isinstance(data, dict) else {}

            for run in runs:
                row = render_run(run, getattr(self.client, "dashboard_url", DEFAULT_DASHBOARD_URL))
                fingerprint = _hash(row)
                if self._changed(row["id"], fingerprint):
                    yield self._emit(row, fingerprint)

            next_cursor = pagination.get("next") if isinstance(pagination, dict) else None
            if not next_cursor or len(runs) < PAGE_SIZE:
                self.state.pop("runs_cursor", None)
                break
            params["cursor"] = next_cursor
            self.state["runs_cursor"] = next_cursor

    def _close_cycle(self) -> None:
        """Find deleted IDs from full scans and queue them for deletion."""
        missing = set(self.fingerprints) - set(self.seen)
        # Only tombstone task and schedule entities; historical runs are kept for audit
        tombstoneable = [mid for mid in missing if mid.startswith(("task:", "schedule:"))]
        self.state["pending_deletes"] = sorted(tombstoneable)
        self.seen.clear()

    def _forget(self) -> Iterator[dict[str, Any]]:
        pending = self.state.setdefault("pending_deletes", [])
        while pending:
            row_id = pending[0]
            self.fingerprints.pop(row_id, None)
            self.stats["deleted"] = self.stats.get("deleted", 0) + 1
            yield {"id": row_id, "_deleted": True}
            pending.pop(0)


def _iter_rows(
    client: TriggerDevClient,
    state: dict[str, Any],
    stats: dict[str, int],
    *,
    include: tuple[str, ...],
    max_requests: int = DEFAULT_MAX_REQUESTS,
    check_active: Callable[[], None] | None = None,
) -> Iterator[dict[str, Any]]:
    walker = _Walker(
        client,
        state,
        stats,
        include=include,
        max_requests=max_requests,
        check_active=check_active,
    )
    try:
        yield from walker.rows()
    except _CutShortError as cut:
        stats["failed"] = max(1, stats.get("failed", 0))
        stats[f"failed_{cut.reason}"] = 1
        logger.warning("Trigger.dev sync stopped early (%s); it will resume on next run.", cut.reason)


def triggerdev_source(
    *,
    resource_name: str,
    api_key: str | None = None,
    api_url: str = DEFAULT_API_URL,
    dashboard_url: str = DEFAULT_DASHBOARD_URL,
    service: Any = None,
    check_active: Callable[[], None] | None = None,
    include: tuple[str, ...] = KINDS,
    max_requests: int = DEFAULT_MAX_REQUESTS,
):
    """Return a dlt resource yielding Trigger.dev tasks, runs, and schedules for ``cognee.remember``.

    Args:
        resource_name: Stable name for this connector instance (e.g. "triggerdev_prod").
            Scopes the sync state and orphan deletion ledger.
        api_key: Trigger.dev Project API token (e.g. "tr_prod_...").
        api_url: Trigger.dev API base URL (defaults to "https://api.trigger.dev").
        dashboard_url: Trigger.dev Cloud dashboard URL for document links.
        service: Injected client instance for testing.
        check_active: Optional authorization checkpoint callback.
        include: Kinds of records to sync: ("tasks", "runs", "schedules").
        max_requests: Per-run request budget cap.

    Returns:
        A dlt resource configured with ``primary_key="id"``, ``write_disposition="merge"``,
        and ``_deleted`` hard-delete column.
    """
    import dlt

    if getattr(dlt_utils, "DOCUMENT_SYNC_VERSION", 0) < 1:
        raise RuntimeError("Trigger.dev connector requires cognee>=1.6.3 (table-scoped document cleanup).")
    if not isinstance(resource_name, str) or not resource_name.strip():
        raise ValueError("triggerdev_source requires a non-empty resource_name")
    if service is None and not api_key:
        raise ValueError("triggerdev_source requires an api_key or a service instance")

    include = tuple(include)
    if not include or set(include) - set(KINDS):
        raise ValueError(f"include must be a non-empty subset of {KINDS}")

    client = service if service is not None else TriggerDevClient(api_key or "", api_url=api_url, dashboard_url=dashboard_url)
    stats: dict[str, int] = {}

    @dlt.resource(
        name=resource_name,
        primary_key="id",
        write_disposition="merge",
        columns={"_deleted": {"data_type": "bool", "hard_delete": True}},
    )
    def triggerdev_workspace():
        stats.clear()
        stats.update(scanned=0, skipped=0, deleted=0, failed=0)
        rows = _iter_rows(
            client,
            dlt.current.resource_state(),
            stats,
            include=include,
            max_requests=max_requests,
            check_active=check_active,
        )
        yield from dlt_utils.guarded_rows(rows, check_active)

    resource = triggerdev_workspace()
    setattr(resource, DOCUMENT_SOURCE_ATTR, "triggerdev")
    setattr(resource, dlt_utils.PIPELINE_SCOPE_ATTR, resource_name)
    resource.cognee_sync_stats = stats
    return resource
