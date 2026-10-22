"""Inngest connector for cognee: a dlt resource that syncs durable workflow functions, execution runs, and triggers into memory.

Usage::

    await cognee.remember(
        inngest_source(api_key="<inngest signing/management key>", resource_name="inngest_prod"),
        dataset_name="workflows_memory",
        primary_key="id",
        write_disposition="merge",
    )
"""

import hashlib
import json
import re
import time
from collections.abc import Callable, Iterator
from typing import Any

import requests
from cognee.shared.logging_utils import get_logger
from cognee.tasks.ingestion import dlt_utils
from cognee.tasks.ingestion.dlt_utils import DOCUMENT_SOURCE_ATTR

logger = get_logger("inngest_connector")

DEFAULT_API_URL = "https://api.inngest.com/v1"
DEFAULT_DASHBOARD_URL = "https://app.inngest.com"

KINDS = ("functions", "runs", "events")
PAGE_SIZE = 100
DEFAULT_MAX_REQUESTS = 500
_TIMEOUT = (10, 30)
_RETRIES = 3

_SECRET_PATTERNS = [
    re.compile(r"(signkey-(?:prod|test)-[A-Za-z0-9_\-]{8,})"),
    re.compile(r"(Bearer\s+[A-Za-z0-9_\-\.]{8,})", re.IGNORECASE),
    re.compile(r"(?:api[_-]?key|secret|password|token)\s*[:=]\s*['\"]?([A-Za-z0-9_\-\.]{6,})['\"]?", re.IGNORECASE),
]


def sanitize_text(text: str | None) -> str:
    """Scrub sensitive keys or credentials from text."""
    if not text:
        return ""
    res = text
    for p in _SECRET_PATTERNS:
        res = p.sub("[REDACTED]", res)
    return res


class InngestSourceError(RuntimeError):
    """Base exception for Inngest connector errors."""


class InngestAPIError(InngestSourceError):
    """API communication failure or unexpected response."""


class InngestAuthError(InngestSourceError):
    """Inngest rejected the API key (HTTP 401)."""


class InngestAccessError(InngestSourceError):
    """Access forbidden for the requested resource (HTTP 403)."""


class InngestRateLimitedError(InngestSourceError):
    """Rate limit exceeded (HTTP 429)."""


class InngestNotFoundError(InngestAPIError):
    """Resource not found (HTTP 404)."""


class _CutShortError(Exception):
    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


class InngestClient:
    """HTTP client for Inngest Management REST API v1."""

    def __init__(
        self,
        api_key: str,
        api_url: str = DEFAULT_API_URL,
        dashboard_url: str = DEFAULT_DASHBOARD_URL,
        sleep: Callable[[float], None] = time.sleep,
    ):
        key = (api_key or "").strip()
        if not key:
            raise ValueError("The Inngest API key is empty or has an invalid format")
        self._api_key = key
        self.api_url = api_url.rstrip("/")
        self.dashboard_url = dashboard_url.rstrip("/")
        self._sleep = sleep
        self.rate_limit: dict[str, int | None] = {}

    def __repr__(self) -> str:
        return f"InngestClient(api_url='{self.api_url}', key=<redacted>)"

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
                    raise InngestAPIError(f"Inngest request failed: {type(exc).__name__}") from None
                self._sleep(2**attempt)
                continue

            if response.status_code >= 500 and not last:
                self._sleep(2**attempt)
                continue

            return self._parse(response, path)

    def _parse(self, response: requests.Response, path: str) -> Any:
        status = response.status_code
        if status == 401:
            raise InngestAuthError("Inngest rejected the API key (HTTP 401)")
        if status == 403:
            raise InngestAccessError(f"Inngest access forbidden for {path} (HTTP 403)")
        if status == 429:
            retry_after = response.headers.get("Retry-After")
            msg = f"Inngest rate limit exceeded (HTTP 429), retry after {retry_after}s" if retry_after else "Inngest rate limit exceeded (HTTP 429)"
            raise InngestRateLimitedError(msg)
        if status == 404:
            raise InngestNotFoundError(f"Inngest resource not found (HTTP 404): {path}")

        try:
            payload = response.json()
        except ValueError:
            payload = None

        if status not in (200, 201):
            raise InngestAPIError(f"Inngest API request failed with HTTP {status}: {response.text[:200]}")

        return payload


def render_function(fn: dict[str, Any], dashboard_url: str = DEFAULT_DASHBOARD_URL) -> dict[str, Any]:
    """Render a function definition into a document."""
    fn_id = str(fn.get("id") or fn.get("slug") or "unnamed_function")
    name = fn.get("name") or fn.get("slug") or fn_id
    title = f"Inngest Function: {name}"

    triggers_info = []
    triggers = fn.get("triggers") or []
    for t in triggers:
        if isinstance(t, dict):
            if "event" in t:
                triggers_info.append(f"Event: {t['event']}")
            elif "cron" in t:
                triggers_info.append(f"Cron: {t['cron']}")
        elif isinstance(t, str):
            triggers_info.append(t)

    fields = [
        f"Function Name: {name}",
        f"Function ID: {fn_id}",
        f"App ID: {fn.get('app_id') or fn.get('app') or 'default'}",
        f"Triggers: {', '.join(triggers_info) if triggers_info else 'Manual'}",
        f"Concurrency: {fn.get('concurrency') or 'Unlimited'}",
    ]

    return {
        "id": f"function:{fn_id}",
        "title": title,
        "content": sanitize_text("\n".join(fields)),
        "url": f"{dashboard_url}/functions/{fn_id}",
        "_deleted": False,
    }


def render_run(run: dict[str, Any], dashboard_url: str = DEFAULT_DASHBOARD_URL) -> dict[str, Any]:
    """Render a workflow run into a document with error and timing telemetry."""
    run_id = str(run.get("run_id") or run.get("id") or "")
    fn_id = run.get("function_id") or run.get("function_slug") or "unknown_function"
    status = run.get("status") or "UNKNOWN"
    title = f"Function Run: {fn_id} - {status}"

    fields = [
        f"Function: {fn_id}",
        f"Run ID: {run_id}",
        f"Status: {status}",
        f"Started At: {run.get('started_at') or 'N/A'}",
        f"Ended At: {run.get('ended_at') or 'N/A'}",
    ]

    error = run.get("error")
    if error and isinstance(error, dict):
        err_msg = error.get("message") or "Unknown error"
        fields.append(f"Error Message: {err_msg}")
        stack = error.get("stack")
        if stack:
            fields.append(f"Stacktrace:\n{stack}")
    elif error:
        fields.append(f"Error: {error}")

    output = run.get("output")
    if output:
        out_str = json.dumps(output) if isinstance(output, (dict, list)) else str(output)
        if len(out_str) > 1000:
            out_str = out_str[:1000] + "... [truncated]"
        fields.append(f"Output Preview: {out_str}")

    return {
        "id": f"run:{run_id}",
        "title": title,
        "content": sanitize_text("\n".join(fields)),
        "url": f"{dashboard_url}/runs/{run_id}",
        "_deleted": False,
    }


def render_event(evt: dict[str, Any], dashboard_url: str = DEFAULT_DASHBOARD_URL) -> dict[str, Any]:
    """Render an event trigger definition into a document."""
    evt_name = evt.get("name") or "unnamed_event"
    evt_id = evt.get("id") or f"{evt_name}_{evt.get('timestamp', '')}"
    title = f"Inngest Event: {evt_name}"

    fields = [
        f"Event Name: {evt_name}",
        f"Event ID: {evt_id}",
        f"Timestamp: {evt.get('timestamp') or 'N/A'}",
    ]

    return {
        "id": f"event:{evt_id}",
        "title": title,
        "content": sanitize_text("\n".join(fields)),
        "url": f"{dashboard_url}/events/{evt_name}",
        "_deleted": False,
    }


def _hash(val: Any) -> str:
    serialized = json.dumps(val, sort_keys=True, default=str)
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()[:16]


class _Walker:
    def __init__(
        self,
        client: InngestClient,
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
        yield from self._forget()

        if "functions" in self.include:
            yield from self._function_rows()
        if "events" in self.include:
            yield from self._event_rows()
        if "runs" in self.include:
            yield from self._run_rows()

        self._close_cycle()
        yield from self._forget()

    def _function_rows(self) -> Iterator[dict[str, Any]]:
        try:
            data = self._call("GET", "/v1/functions")
            functions = data.get("data") or data.get("functions") or []
        except _CutShortError:
            raise
        except Exception as exc:
            logger.warning("Failed to fetch Inngest functions: %s", exc)
            functions = []

        for fn in functions:
            row = render_function(fn, getattr(self.client, "dashboard_url", DEFAULT_DASHBOARD_URL))
            fp = _hash(row)
            if self._changed(row["id"], fp):
                yield self._emit(row, fp)

    def _event_rows(self) -> Iterator[dict[str, Any]]:
        try:
            data = self._call("GET", "/v1/events")
            events = data.get("data") or data.get("events") or []
        except _CutShortError:
            raise
        except Exception as exc:
            logger.warning("Failed to fetch Inngest events: %s", exc)
            events = []

        for evt in events:
            row = render_event(evt, getattr(self.client, "dashboard_url", DEFAULT_DASHBOARD_URL))
            fp = _hash(row)
            if self._changed(row["id"], fp):
                yield self._emit(row, fp)

    def _run_rows(self) -> Iterator[dict[str, Any]]:
        cursor = self.state.get("runs_cursor")
        params: dict[str, Any] = {"limit": PAGE_SIZE}
        if cursor:
            params["cursor"] = cursor

        while True:
            data = self._call("GET", "/v1/runs", params=params)
            runs = data.get("data") or data.get("runs") or []
            pagination = data.get("pagination") if isinstance(data, dict) else {}

            for run in runs:
                row = render_run(run, getattr(self.client, "dashboard_url", DEFAULT_DASHBOARD_URL))
                fp = _hash(row)
                if self._changed(row["id"], fp):
                    yield self._emit(row, fp)

            next_cursor = pagination.get("next") if isinstance(pagination, dict) else None
            if not next_cursor or len(runs) < PAGE_SIZE:
                self.state.pop("runs_cursor", None)
                break
            params["cursor"] = next_cursor
            self.state["runs_cursor"] = next_cursor

    def _close_cycle(self) -> None:
        missing = set(self.fingerprints) - set(self.seen)
        tombstoneable = [mid for mid in missing if mid.startswith(("function:", "event:"))]
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
    client: InngestClient,
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
        logger.warning("Inngest sync stopped early (%s); it will resume on next run.", cut.reason)


def inngest_source(
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
    """Return a dlt resource yielding Inngest functions, workflow runs, and events for ``cognee.remember``.

    Args:
        resource_name: Unique identifier for this connection instance.
        api_key: Inngest Signing Key or Management API key.
        api_url: Custom Inngest API base URL.
        dashboard_url: Web dashboard URL for document references.
        service: Injected client instance for testing.
        check_active: Optional authorization checkpoint callback.
        include: Kinds of records to sync: ("functions", "runs", "events").
        max_requests: Per-run request budget cap.

    Returns:
        A dlt resource configured with ``primary_key="id"``, ``write_disposition="merge"``,
        and ``_deleted`` hard-delete column.
    """
    import dlt

    if getattr(dlt_utils, "DOCUMENT_SYNC_VERSION", 0) < 1:
        raise RuntimeError("Inngest connector requires cognee>=1.6.3 (table-scoped document cleanup).")
    if not isinstance(resource_name, str) or not resource_name.strip():
        raise ValueError("inngest_source requires a non-empty resource_name")
    if service is None and not api_key:
        raise ValueError("inngest_source requires an api_key or a service instance")

    include = tuple(include)
    if not include or set(include) - set(KINDS):
        raise ValueError(f"include must be a non-empty subset of {KINDS}")

    client = service if service is not None else InngestClient(api_key or "", api_url=api_url, dashboard_url=dashboard_url)
    stats: dict[str, int] = {}

    @dlt.resource(
        name=resource_name,
        primary_key="id",
        write_disposition="merge",
        columns={"_deleted": {"data_type": "bool", "hard_delete": True}},
    )
    def inngest_workplace():
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

    resource = inngest_workplace()
    setattr(resource, DOCUMENT_SOURCE_ATTR, "inngest")
    setattr(resource, dlt_utils.PIPELINE_SCOPE_ATTR, resource_name)
    resource.cognee_sync_stats = stats
    return resource
