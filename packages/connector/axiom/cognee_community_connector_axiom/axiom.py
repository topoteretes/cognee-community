"""Axiom connector for cognee: a dlt resource that syncs datasets, monitors, alert queries, and notifiers into memory.

Usage::

    await cognee.remember(
        axiom_source(api_key="xapt-...", resource_name="axiom_prod"),
        dataset_name="observability_memory",
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

logger = get_logger("axiom_connector")

DEFAULT_API_URL = "https://api.axiom.co/v1"
DEFAULT_DASHBOARD_URL = "https://app.axiom.co"

KINDS = ("datasets", "monitors", "notifiers")
PAGE_SIZE = 100
DEFAULT_MAX_REQUESTS = 500
_TIMEOUT = (10, 30)
_RETRIES = 3

_SECRET_PATTERNS = [
    re.compile(r"(xapt-[A-Za-z0-9_\-]{8,})"),
    re.compile(r"(Bearer\s+[A-Za-z0-9_\-\.]{8,})", re.IGNORECASE),
    re.compile(r"(?:api[_-]?key|secret|password|token)\s*[:=]\s*['\"]?([A-Za-z0-9_\-\.]{6,})['\"]?", re.IGNORECASE),
]


def sanitize_text(text: str | None) -> str:
    """Scrub sensitive keys, tokens, or credentials from text."""
    if not text:
        return ""
    res = text
    for p in _SECRET_PATTERNS:
        res = p.sub("[REDACTED]", res)
    return res


class AxiomSourceError(RuntimeError):
    """Base exception for Axiom connector errors."""


class AxiomAPIError(AxiomSourceError):
    """API communication failure or unexpected response."""


class AxiomAuthError(AxiomSourceError):
    """Axiom rejected the API key (HTTP 401)."""


class AxiomAccessError(AxiomSourceError):
    """Access forbidden for the requested resource (HTTP 403)."""


class AxiomRateLimitedError(AxiomSourceError):
    """Rate limit exceeded (HTTP 429)."""


class AxiomNotFoundError(AxiomAPIError):
    """Resource not found (HTTP 404)."""


class _CutShortError(Exception):
    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


class AxiomClient:
    """HTTP client for Axiom REST API v1."""

    def __init__(
        self,
        api_key: str,
        api_url: str = DEFAULT_API_URL,
        dashboard_url: str = DEFAULT_DASHBOARD_URL,
        sleep: Callable[[float], None] = time.sleep,
    ):
        key = (api_key or "").strip()
        if not key:
            raise ValueError("The Axiom API key is empty or has an invalid format")
        self._api_key = key
        self.api_url = api_url.rstrip("/")
        self.dashboard_url = dashboard_url.rstrip("/")
        self._sleep = sleep
        self.rate_limit: dict[str, int | None] = {}

    def __repr__(self) -> str:
        return f"AxiomClient(api_url='{self.api_url}', key=<redacted>)"

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
                    raise AxiomAPIError(f"Axiom request failed: {type(exc).__name__}") from None
                self._sleep(2**attempt)
                continue

            if response.status_code >= 500 and not last:
                self._sleep(2**attempt)
                continue

            return self._parse(response, path)

    def _parse(self, response: requests.Response, path: str) -> Any:
        status = response.status_code
        if status == 401:
            raise AxiomAuthError("Axiom rejected the API key (HTTP 401)")
        if status == 403:
            raise AxiomAccessError(f"Axiom access forbidden for {path} (HTTP 403)")
        if status == 429:
            retry_after = response.headers.get("Retry-After")
            msg = f"Axiom rate limit exceeded (HTTP 429), retry after {retry_after}s" if retry_after else "Axiom rate limit exceeded (HTTP 429)"
            raise AxiomRateLimitedError(msg)
        if status == 404:
            raise AxiomNotFoundError(f"Axiom resource not found (HTTP 404): {path}")

        try:
            payload = response.json()
        except ValueError:
            payload = None

        if status not in (200, 201):
            raise AxiomAPIError(f"Axiom API request failed with HTTP {status}: {response.text[:200]}")

        return payload


def render_dataset(ds: dict[str, Any], dashboard_url: str = DEFAULT_DASHBOARD_URL) -> dict[str, Any]:
    """Render an Axiom dataset into a document."""
    ds_id = str(ds.get("id") or ds.get("name") or "unnamed_dataset")
    name = ds.get("name") or ds_id
    title = f"Axiom Dataset: {name}"

    fields = [
        f"Dataset Name: {name}",
        f"Dataset ID: {ds_id}",
        f"Description: {ds.get('description') or 'No description provided'}",
        f"Retention Days: {ds.get('retentionDays') or ds.get('retention_days') or 'Default'}",
        f"Created At: {ds.get('created') or ds.get('created_at') or 'N/A'}",
    ]

    return {
        "id": f"dataset:{ds_id}",
        "title": title,
        "content": sanitize_text("\n".join(fields)),
        "url": f"{dashboard_url}/datasets/{name}",
        "_deleted": False,
    }


def render_monitor(mon: dict[str, Any], dashboard_url: str = DEFAULT_DASHBOARD_URL) -> dict[str, Any]:
    """Render an Axiom monitor/alert into a document."""
    mon_id = str(mon.get("id") or "unnamed_monitor")
    name = mon.get("name") or mon_id
    disabled = mon.get("disabledUntil") is not None or mon.get("disabled", False)
    status_str = "Disabled" if disabled else "Active"
    title = f"Axiom Monitor: {name} [{status_str}]"

    fields = [
        f"Monitor Name: {name}",
        f"Monitor ID: {mon_id}",
        f"Status: {status_str}",
        f"Description: {mon.get('description') or 'No description'}",
        f"APL Query:\n{mon.get('aplQuery') or mon.get('apl') or 'N/A'}",
        f"Interval: {mon.get('interval') or 'N/A'}",
        f"Operator: {mon.get('operator') or 'N/A'}",
        f"Threshold: {mon.get('threshold') if 'threshold' in mon else 'N/A'}",
    ]

    return {
        "id": f"monitor:{mon_id}",
        "title": title,
        "content": sanitize_text("\n".join(fields)),
        "url": f"{dashboard_url}/monitors/{mon_id}",
        "_deleted": False,
    }


def render_notifier(notif: dict[str, Any], dashboard_url: str = DEFAULT_DASHBOARD_URL) -> dict[str, Any]:
    """Render an Axiom notification channel into a document."""
    notif_id = str(notif.get("id") or "unnamed_notifier")
    name = notif.get("name") or notif_id
    notif_type = notif.get("type") or "webhook"
    title = f"Axiom Notifier: {name} ({notif_type})"

    fields = [
        f"Notifier Name: {name}",
        f"Notifier ID: {notif_id}",
        f"Type: {notif_type}",
        f"Description: {notif.get('description') or 'No description'}",
    ]

    return {
        "id": f"notifier:{notif_id}",
        "title": title,
        "content": sanitize_text("\n".join(fields)),
        "url": f"{dashboard_url}/settings/notifiers/{notif_id}",
        "_deleted": False,
    }


def _hash(val: Any) -> str:
    serialized = json.dumps(val, sort_keys=True, default=str)
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()[:16]


class _Walker:
    def __init__(
        self,
        client: AxiomClient,
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

        if "datasets" in self.include:
            yield from self._dataset_rows()
        if "monitors" in self.include:
            yield from self._monitor_rows()
        if "notifiers" in self.include:
            yield from self._notifier_rows()

        self._close_cycle()
        yield from self._forget()

    def _dataset_rows(self) -> Iterator[dict[str, Any]]:
        try:
            data = self._call("GET", "/v1/datasets")
            datasets = data if isinstance(data, list) else data.get("datasets", [])
        except _CutShortError:
            raise
        except Exception as exc:
            logger.warning("Failed to fetch Axiom datasets: %s", exc)
            datasets = []

        for ds in datasets:
            row = render_dataset(ds, getattr(self.client, "dashboard_url", DEFAULT_DASHBOARD_URL))
            fp = _hash(row)
            if self._changed(row["id"], fp):
                yield self._emit(row, fp)

    def _monitor_rows(self) -> Iterator[dict[str, Any]]:
        try:
            data = self._call("GET", "/v1/monitors")
            monitors = data if isinstance(data, list) else data.get("monitors", [])
        except _CutShortError:
            raise
        except Exception as exc:
            logger.warning("Failed to fetch Axiom monitors: %s", exc)
            monitors = []

        for mon in monitors:
            row = render_monitor(mon, getattr(self.client, "dashboard_url", DEFAULT_DASHBOARD_URL))
            fp = _hash(row)
            if self._changed(row["id"], fp):
                yield self._emit(row, fp)

    def _notifier_rows(self) -> Iterator[dict[str, Any]]:
        try:
            data = self._call("GET", "/v1/notifiers")
            notifiers = data if isinstance(data, list) else data.get("notifiers", [])
        except _CutShortError:
            raise
        except Exception as exc:
            logger.warning("Failed to fetch Axiom notifiers: %s", exc)
            notifiers = []

        for notif in notifiers:
            row = render_notifier(notif, getattr(self.client, "dashboard_url", DEFAULT_DASHBOARD_URL))
            fp = _hash(row)
            if self._changed(row["id"], fp):
                yield self._emit(row, fp)

    def _close_cycle(self) -> None:
        missing = set(self.fingerprints) - set(self.seen)
        self.state["pending_deletes"] = sorted(missing)
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
    client: AxiomClient,
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
        logger.warning("Axiom sync stopped early (%s); it will resume on next run.", cut.reason)


def axiom_source(
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
    """Return a dlt resource yielding Axiom datasets, monitors, and notifiers for ``cognee.remember``.

    Args:
        resource_name: Unique identifier for this connection instance.
        api_key: Axiom Personal or API Token (`xapt-...`).
        api_url: Custom Axiom API base URL.
        dashboard_url: Web dashboard URL for document references.
        service: Injected client instance for testing.
        check_active: Optional authorization checkpoint callback.
        include: Kinds of records to sync: ("datasets", "monitors", "notifiers").
        max_requests: Per-run request budget cap.

    Returns:
        A dlt resource configured with ``primary_key="id"``, ``write_disposition="merge"``,
        and ``_deleted`` hard-delete column.
    """
    import dlt

    if getattr(dlt_utils, "DOCUMENT_SYNC_VERSION", 0) < 1:
        raise RuntimeError("Axiom connector requires cognee>=1.6.3 (table-scoped document cleanup).")
    if not isinstance(resource_name, str) or not resource_name.strip():
        raise ValueError("axiom_source requires a non-empty resource_name")
    if service is None and not api_key:
        raise ValueError("axiom_source requires an api_key or a service instance")

    include = tuple(include)
    if not include or set(include) - set(KINDS):
        raise ValueError(f"include must be a non-empty subset of {KINDS}")

    client = service if service is not None else AxiomClient(api_key or "", api_url=api_url, dashboard_url=dashboard_url)
    stats: dict[str, int] = {}

    @dlt.resource(
        name=resource_name,
        primary_key="id",
        write_disposition="merge",
        columns={"_deleted": {"data_type": "bool", "hard_delete": True}},
    )
    def axiom_workplace():
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

    resource = axiom_workplace()
    setattr(resource, DOCUMENT_SOURCE_ATTR, "axiom")
    setattr(resource, dlt_utils.PIPELINE_SCOPE_ATTR, resource_name)
    resource.cognee_sync_stats = stats
    return resource
