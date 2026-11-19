"""Novu notification infrastructure data-source connector for Cognee.

Syncs notification workflows, topics, notification groups, and integration providers
from Novu into Cognee memory documents with incremental fingerprinting and deletion
tombstones (forget-on-delete).
"""

from __future__ import annotations

import hashlib
import json
import time
from collections.abc import Callable, Iterable, Iterator, Sequence
from typing import Any
from urllib.parse import urlparse

import dlt
from cognee.shared.logging_utils import get_logger
from cognee.tasks.ingestion import dlt_utils
from cognee.tasks.ingestion.dlt_utils import DOCUMENT_SOURCE_ATTR

logger = get_logger("novu_connector")

DEFAULT_NOVU_BASE_URL = "https://api.novu.co"
DEFAULT_DASHBOARD_URL = "https://dashboard.novu.co"
DEFAULT_PAGE_LIMIT = 50
DEFAULT_RETRIES = 3
DEFAULT_BACKOFF_FACTOR = 0.5
RETRYABLE_STATUSES = {429, 500, 502, 503, 504}

VALID_INCLUDES = ("workflows", "topics", "notification_groups", "integrations")
ROW_COLUMNS = ("id", "title", "content", "url", "_deleted")

NOVU_REMEMBER_KWARGS: dict[str, Any] = {
    "primary_key": "id",
    "write_disposition": "merge",
}


class NovuError(Exception):
    """Base exception for all Novu connector errors."""


class NovuAuthError(NovuError):
    """Raised when authentication fails (HTTP 401)."""


class NovuAccessError(NovuError):
    """Raised when access is forbidden (HTTP 403)."""


class NovuNotFoundError(NovuError):
    """Raised when a workflow, topic, or integration is not found (HTTP 404)."""


class NovuRateLimitedError(NovuError):
    """Raised when the Novu API rate limit is reached (HTTP 429)."""


class NovuAPIError(NovuError):
    """Raised on unexpected HTTP status or malformed response payload."""


class NovuClient:
    """Resilient HTTP client for the Novu REST API with retry and pagination support."""

    def __init__(
        self,
        api_key: str,
        *,
        base_url: str = DEFAULT_NOVU_BASE_URL,
        session: Any = None,
        retries: int = DEFAULT_RETRIES,
        backoff_factor: float = DEFAULT_BACKOFF_FACTOR,
    ) -> None:
        self.api_key = self._validate_key(api_key)
        self.base_url = self._validate_base_url(base_url)
        self.retries = max(0, retries)
        self.backoff_factor = max(0.0, backoff_factor)

        if session is not None:
            self._session = session
        else:
            import requests

            self._session = requests.Session()

    @staticmethod
    def _validate_key(key: str) -> str:
        if not isinstance(key, str):
            raise ValueError(f"api_key must be a string, got {type(key).__name__}")
        cleaned = key.strip()
        if not cleaned or not cleaned.isascii() or not cleaned.isprintable() or " " in cleaned:
            raise ValueError("api_key must be a non-empty, printable ASCII string without spaces")
        return cleaned

    @staticmethod
    def _validate_base_url(url: str) -> str:
        if not isinstance(url, str):
            raise ValueError(f"base_url must be a string, got {type(url).__name__}")
        cleaned = url.strip().rstrip("/")
        parsed = urlparse(cleaned)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError(f"base_url must be an absolute http(s) URL, got {url!r}")
        return cleaned

    def request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json_body: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        url = f"{self.base_url}/{path.lstrip('/')}"
        headers = {
            "Authorization": f"ApiKey {self.api_key}",
            "Accept": "application/json",
            "Content-Type": "application/json",
        }

        attempt = 0
        while True:
            try:
                resp = self._session.request(
                    method=method.upper(),
                    url=url,
                    headers=headers,
                    params=params,
                    json=json_body,
                    timeout=30,
                )
            except Exception as exc:
                if attempt >= self.retries:
                    raise NovuAPIError(
                        f"Novu request to {path} failed after {attempt} retries: {exc}"
                    ) from exc
                attempt += 1
                time.sleep(self.backoff_factor * (2 ** (attempt - 1)))
                continue

            status = resp.status_code
            if status in (200, 201):
                try:
                    return resp.json()
                except Exception as exc:
                    raise NovuAPIError(f"Novu {path} returned non-JSON payload") from exc

            if status in RETRYABLE_STATUSES and attempt < self.retries:
                attempt += 1
                retry_after = resp.headers.get("Retry-After")
                sleep_sec = (
                    float(retry_after)
                    if retry_after
                    else self.backoff_factor * (2 ** (attempt - 1))
                )
                time.sleep(sleep_sec)
                continue

            self._map_http_error(status, path, resp.text)

    @staticmethod
    def _map_http_error(status: int, path: str, body: str) -> None:
        if status == 401:
            raise NovuAuthError(f"Novu authentication failed (HTTP 401) on {path}: {body}")
        if status == 403:
            raise NovuAccessError(f"Novu access forbidden (HTTP 403) on {path}: {body}")
        if status == 404:
            raise NovuNotFoundError(f"Novu resource not found (HTTP 404) on {path}: {body}")
        if status == 429:
            raise NovuRateLimitedError(f"Novu rate limit exceeded (HTTP 429) on {path}: {body}")
        raise NovuAPIError(f"Novu request to {path} failed with HTTP {status}: {body}")


def _text(val: Any) -> str:
    if val is None:
        return ""
    if isinstance(val, (int, float, bool)):
        return str(val)
    return str(val).strip()


def _lines(pairs: Iterable[tuple[str, str]]) -> list[str]:
    return [f"{label}: {val}" for label, val in pairs if val]


def _hash(obj: Any) -> str:
    dumped = json.dumps(obj, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(dumped.encode("utf-8")).hexdigest()


def render_workflow(wf: dict[str, Any], dashboard_url: str) -> dict[str, Any]:
    wf_id = _text(wf.get("_id") or wf.get("id"))
    name = _text(wf.get("name")) or f"Workflow {wf_id}"
    description = _text(wf.get("description")) or "No description provided"
    active = bool(wf.get("active", True))
    status_str = "Active" if active else "Inactive"

    steps = wf.get("steps") or []
    step_types: list[str] = []
    for s in steps:
        template = s.get("template") or {}
        stype = template.get("type") or s.get("type")
        if stype:
            step_types.append(str(stype))
    steps_str = ", ".join(sorted(set(step_types))) if step_types else "None"

    tags = wf.get("tags") or []
    tags_str = ", ".join(sorted(tags)) if tags else "None"

    fields = [
        ("Type", "Novu Notification Workflow"),
        ("Name", name),
        ("Workflow ID", wf_id),
        ("Status", status_str),
        ("Description", description),
        ("Delivery Channels / Steps", steps_str),
        ("Tags", tags_str),
    ]
    return {
        "id": f"workflow:{wf_id}",
        "title": f"Novu Workflow: {name} [{status_str}]",
        "content": "\n".join(_lines(fields)),
        "url": f"{dashboard_url}/workflows/edit/{wf_id}",
        "_deleted": False,
    }


def render_topic(topic: dict[str, Any], dashboard_url: str) -> dict[str, Any]:
    topic_id = _text(topic.get("_id") or topic.get("id"))
    key = _text(topic.get("key")) or topic_id
    name = _text(topic.get("name")) or key
    subscribers = topic.get("subscribers") or []
    sub_count = len(subscribers)

    fields = [
        ("Type", "Novu Notification Topic"),
        ("Topic Name", name),
        ("Topic Key", key),
        ("Topic ID", topic_id),
        ("Subscribers Count", str(sub_count)),
    ]
    return {
        "id": f"topic:{key}",
        "title": f"Novu Topic: {name} ({key})",
        "content": "\n".join(_lines(fields)),
        "url": f"{dashboard_url}/topics/{key}",
        "_deleted": False,
    }


def render_group(group: dict[str, Any], dashboard_url: str) -> dict[str, Any]:
    group_id = _text(group.get("_id") or group.get("id"))
    name = _text(group.get("name")) or f"Group {group_id}"

    fields = [
        ("Type", "Novu Notification Group"),
        ("Group Name", name),
        ("Group ID", group_id),
    ]
    return {
        "id": f"group:{group_id}",
        "title": f"Novu Group: {name}",
        "content": "\n".join(_lines(fields)),
        "url": f"{dashboard_url}/settings/organization",
        "_deleted": False,
    }


def render_integration(integ: dict[str, Any], dashboard_url: str) -> dict[str, Any]:
    integ_id = _text(integ.get("_id") or integ.get("id"))
    provider = _text(integ.get("providerId")) or f"Provider {integ_id}"
    channel = _text(integ.get("channel")) or "generic"
    name = _text(integ.get("name")) or f"{provider.capitalize()} ({channel})"
    active = bool(integ.get("active", True))
    primary = bool(integ.get("primary", False))
    status_str = "Active" if active else "Inactive"
    if primary:
        status_str += " (Primary)"

    fields = [
        ("Type", "Novu Integration Provider"),
        ("Provider ID", provider),
        ("Channel Type", channel),
        ("Status", status_str),
        ("Integration ID", integ_id),
        ("Credentials", "[Redacted & Protected]"),
    ]
    return {
        "id": f"integration:{integ_id}",
        "title": f"Novu Integration: {name} [{channel}]",
        "content": "\n".join(_lines(fields)),
        "url": f"{dashboard_url}/integrations",
        "_deleted": False,
    }


class _NovuStateWalker:
    def __init__(
        self,
        client: Any,
        dashboard_url: str,
        state: dict[str, Any],
        stats: dict[str, int],
        *,
        include: Sequence[str] = VALID_INCLUDES,
    ) -> None:
        self.client = client
        self.dashboard_url = dashboard_url.rstrip("/")
        self.state = state
        self.stats = stats
        self.include = tuple(include)

        self.fingerprints: dict[str, str] = self.state.setdefault("fingerprints", {})
        self.seen: list[str] = []

    def rows(self) -> Iterator[dict[str, Any]]:
        self.stats.setdefault("scanned", 0)
        self.stats.setdefault("emitted", 0)
        self.stats.setdefault("skipped", 0)
        self.stats.setdefault("deleted", 0)
        self.stats.setdefault("failed", 0)

        # 1. Workflows
        if "workflows" in self.include:
            for wf in self._walk_paged("v1/workflows"):
                row = render_workflow(wf, self.dashboard_url)
                fp = _hash(row)
                if self._changed(row["id"], fp):
                    yield self._emit(row, fp)

        # 2. Topics
        if "topics" in self.include:
            for tp in self._walk_paged("v1/topics"):
                row = render_topic(tp, self.dashboard_url)
                fp = _hash(row)
                if self._changed(row["id"], fp):
                    yield self._emit(row, fp)

        # 3. Notification Groups
        if "notification_groups" in self.include:
            res = self.client.request("GET", "v1/notification-groups") or {}
            groups = res.get("data") or []
            for gp in groups:
                row = render_group(gp, self.dashboard_url)
                fp = _hash(row)
                if self._changed(row["id"], fp):
                    yield self._emit(row, fp)

        # 4. Integrations
        if "integrations" in self.include:
            res = self.client.request("GET", "v1/integrations") or {}
            integs = res.get("data") or []
            for it in integs:
                row = render_integration(it, self.dashboard_url)
                fp = _hash(row)
                if self._changed(row["id"], fp):
                    yield self._emit(row, fp)

        # 5. Reconcile deletions
        self._close_cycle()
        yield from self._forget()

    def _walk_paged(self, endpoint: str) -> Iterator[dict[str, Any]]:
        page = 0
        while True:
            params = {"page": page, "limit": DEFAULT_PAGE_LIMIT}
            res = self.client.request("GET", endpoint, params=params) or {}
            items = res.get("data") or []
            yield from items
            if len(items) < DEFAULT_PAGE_LIMIT:
                break
            page += 1

    def _changed(self, row_id: str, fingerprint: str) -> bool:
        self.seen.append(row_id)
        self.stats["scanned"] += 1
        if self.fingerprints.get(row_id) == fingerprint:
            self.stats["skipped"] += 1
            return False
        return True

    def _emit(self, row: dict[str, Any], fingerprint: str) -> dict[str, Any]:
        self.fingerprints[row["id"]] = fingerprint
        self.stats["emitted"] += 1
        return row

    def _close_cycle(self) -> None:
        missing = set(self.fingerprints) - set(self.seen)
        self.state["pending_deletes"] = sorted(missing)
        self.seen.clear()

    def _forget(self) -> Iterator[dict[str, Any]]:
        pending = self.state.setdefault("pending_deletes", [])
        while pending:
            row_id = pending[0]
            try:
                gone = self._is_gone(row_id)
            except (NovuAPIError, NovuAccessError) as exc:
                self.stats["failed"] += 1
                logger.warning("Novu deletion check failed, will retry next run: %s", exc)
                gone = False

            if gone:
                self.fingerprints.pop(row_id, None)
                self.stats["deleted"] += 1
                yield {"id": row_id, "_deleted": True}
            pending.pop(0)

    def _is_gone(self, row_id: str) -> bool:
        parts = row_id.split(":", 1)
        prefix, key = parts[0], parts[1]
        if prefix == "workflow":
            if "workflows" not in self.include:
                return True
            try:
                self.client.request("GET", f"v1/workflows/{key}")
                return False
            except NovuNotFoundError:
                return True
        elif prefix == "topic":
            if "topics" not in self.include:
                return True
            try:
                self.client.request("GET", f"v1/topics/{key}")
                return False
            except NovuNotFoundError:
                return True
        elif prefix == "group":
            if "notification_groups" not in self.include:
                return True
            res = self.client.request("GET", "v1/notification-groups") or {}
            groups = res.get("data") or []
            return not any(str(g.get("_id") or g.get("id")) == key for g in groups)
        elif prefix == "integration":
            if "integrations" not in self.include:
                return True
            res = self.client.request("GET", "v1/integrations") or {}
            integs = res.get("data") or []
            return not any(str(it.get("_id") or it.get("id")) == key for it in integs)
        return True


def _iter_rows(
    client: Any,
    dashboard_url: str,
    state: dict[str, Any],
    stats: dict[str, int],
    **kwargs: Any,
) -> Iterator[dict[str, Any]]:
    walker = _NovuStateWalker(client, dashboard_url, state, stats, **kwargs)
    yield from walker.rows()


def novu_source(
    *,
    resource_name: str,
    api_key: str | None = None,
    base_url: str = DEFAULT_NOVU_BASE_URL,
    dashboard_url: str = DEFAULT_DASHBOARD_URL,
    service: Any = None,
    check_active: Callable[[], None] | None = None,
    include: Sequence[str] = VALID_INCLUDES,
):
    """Return a dlt resource yielding Novu workflows and infrastructure for cognee.remember."""
    if getattr(dlt_utils, "DOCUMENT_SYNC_VERSION", 0) < 1:
        raise RuntimeError("Novu sync requires cognee>=1.6.3 (table-scoped document cleanup).")
    if not isinstance(resource_name, str) or not resource_name.strip():
        raise ValueError("novu_source needs a valid resource_name")

    include = tuple(include)
    if not include or set(include) - set(VALID_INCLUDES):
        raise ValueError(f"include must be a non-empty subset of {VALID_INCLUDES}")

    client = (
        service if service is not None else NovuClient(api_key=api_key or "", base_url=base_url)
    )
    resolved_dash = (dashboard_url or DEFAULT_DASHBOARD_URL).rstrip("/")
    stats: dict[str, int] = {}

    @dlt.resource(
        name=resource_name,
        primary_key="id",
        write_disposition="merge",
        columns={"_deleted": {"data_type": "bool", "hard_delete": True}},
    )
    def novu_workspace():
        stats.clear()
        stats.update(scanned=0, skipped=0, deleted=0, failed=0)
        rows = _iter_rows(
            client=client,
            dashboard_url=resolved_dash,
            state=dlt.current.resource_state(),
            stats=stats,
            include=include,
        )
        yield from dlt_utils.guarded_rows(rows, check_active)

    res = novu_workspace()
    setattr(res, DOCUMENT_SOURCE_ATTR, "novu")
    setattr(res, dlt_utils.PIPELINE_SCOPE_ATTR, resource_name)
    res.cognee_sync_stats = stats
    return res


novu = novu_source
