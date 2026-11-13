"""Svix webhooks and event infrastructure data-source connector for Cognee.

Syncs applications, endpoint subscriptions, and event type schemas from Svix
into Cognee memory documents with incremental fingerprinting and deletion
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

logger = get_logger("svix_connector")

DEFAULT_SVIX_BASE_URL = "https://api.svix.com"
DEFAULT_DASHBOARD_URL = "https://dashboard.svix.com"
DEFAULT_PAGE_LIMIT = 50
DEFAULT_RETRIES = 3
DEFAULT_BACKOFF_FACTOR = 0.5
RETRYABLE_STATUSES = {429, 500, 502, 503, 504}

VALID_INCLUDES = ("applications", "endpoints", "event_types")
ROW_COLUMNS = ("id", "title", "content", "url", "_deleted")

SVIX_REMEMBER_KWARGS: dict[str, Any] = {
    "primary_key": "id",
    "write_disposition": "merge",
}


class SvixError(Exception):
    """Base exception for all Svix connector errors."""


class SvixAuthError(SvixError):
    """Raised when authentication fails (HTTP 401)."""


class SvixAccessError(SvixError):
    """Raised when access is forbidden (HTTP 403)."""


class SvixNotFoundError(SvixError):
    """Raised when an application, endpoint, or event type is not found (HTTP 404)."""


class SvixRateLimitedError(SvixError):
    """Raised when the Svix API rate limit is reached (HTTP 429)."""


class SvixAPIError(SvixError):
    """Raised on unexpected HTTP status or malformed response payload."""


class SvixClient:
    """Resilient HTTP client for the Svix REST API with retry and cursor support."""

    def __init__(
        self,
        api_token: str,
        *,
        base_url: str = DEFAULT_SVIX_BASE_URL,
        session: Any = None,
        retries: int = DEFAULT_RETRIES,
        backoff_factor: float = DEFAULT_BACKOFF_FACTOR,
    ) -> None:
        self.api_token = self._validate_token(api_token)
        self.base_url = self._validate_base_url(base_url)
        self.retries = max(0, retries)
        self.backoff_factor = max(0.0, backoff_factor)

        if session is not None:
            self._session = session
        else:
            import requests

            self._session = requests.Session()

    @staticmethod
    def _validate_token(token: str) -> str:
        if not isinstance(token, str):
            raise ValueError(f"api_token must be a string, got {type(token).__name__}")
        cleaned = token.strip()
        if not cleaned or not cleaned.isascii() or not cleaned.isprintable() or " " in cleaned:
            raise ValueError("api_token must be a non-empty, printable ASCII string without spaces")
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
            "Authorization": f"Bearer {self.api_token}",
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
                    raise SvixAPIError(
                        f"Svix request to {path} failed after {attempt} retries: {exc}"
                    ) from exc
                attempt += 1
                time.sleep(self.backoff_factor * (2 ** (attempt - 1)))
                continue

            status = resp.status_code
            if status in (200, 201):
                try:
                    return resp.json()
                except Exception as exc:
                    raise SvixAPIError(f"Svix {path} returned non-JSON payload") from exc

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
            raise SvixAuthError(f"Svix authentication failed (HTTP 401) on {path}: {body}")
        if status == 403:
            raise SvixAccessError(f"Svix access forbidden (HTTP 403) on {path}: {body}")
        if status == 404:
            raise SvixNotFoundError(f"Svix resource not found (HTTP 404) on {path}: {body}")
        if status == 429:
            raise SvixRateLimitedError(f"Svix rate limit exceeded (HTTP 429) on {path}: {body}")
        raise SvixAPIError(f"Svix request to {path} failed with HTTP {status}: {body}")


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


def render_application(app: dict[str, Any], dashboard_url: str) -> dict[str, Any]:
    app_id = _text(app["id"])
    name = _text(app.get("name")) or f"Application {app_id}"
    uid = _text(app.get("uid"))
    created_at = _text(app.get("createdAt"))
    rate_limit = _text(app.get("rateLimit"))

    fields = [
        ("Type", "Svix Application"),
        ("Name", name),
        ("Application ID", app_id),
        ("Unique Identifier (UID)", uid),
        ("Created At", created_at),
        ("Rate Limit", f"{rate_limit}/sec" if rate_limit else ""),
    ]
    return {
        "id": f"app:{app_id}",
        "title": f"Svix App: {name} ({uid or app_id})",
        "content": "\n".join(_lines(fields)),
        "url": f"{dashboard_url}/apps/{app_id}",
        "_deleted": False,
    }


def render_endpoint(ep: dict[str, Any], app_id: str, dashboard_url: str) -> dict[str, Any]:
    ep_id = _text(ep["id"])
    url = _text(ep.get("url")) or "https://unknown.target"
    description = _text(ep.get("description")) or f"Endpoint {ep_id}"
    version = _text(ep.get("version")) or "1"
    disabled = bool(ep.get("disabled", False))
    status_str = "Disabled" if disabled else "Active"

    filter_types = ep.get("filterTypes") or []
    filter_str = ", ".join(sorted(filter_types)) if filter_types else "All Event Types"

    channels = ep.get("channels") or []
    channel_str = ", ".join(sorted(channels)) if channels else "Default"

    fields = [
        ("Type", "Svix Webhook Endpoint"),
        ("Endpoint ID", ep_id),
        ("Parent Application ID", app_id),
        ("Target URL", url),
        ("Status", status_str),
        ("Description", description),
        ("API Version", f"v{version}"),
        ("Subscribed Event Types", filter_str),
        ("Subscribed Channels", channel_str),
        ("Endpoint Secret", "[Redacted & Protected]"),
        ("Custom Headers", "[Redacted & Protected]"),
    ]
    return {
        "id": f"endpoint:{app_id}:{ep_id}",
        "title": f"Svix Endpoint: {description} [{status_str}]",
        "content": "\n".join(_lines(fields)),
        "url": f"{dashboard_url}/apps/{app_id}/endpoints/{ep_id}",
        "_deleted": False,
    }


def render_event_type(ev: dict[str, Any], dashboard_url: str) -> dict[str, Any]:
    name = _text(ev["name"])
    description = _text(ev.get("description")) or f"Event {name}"
    archived = bool(ev.get("archived", False))
    status_str = "Archived" if archived else "Active"
    schemas = ev.get("schemas") or {}
    schema_keys = ", ".join(sorted(schemas.keys())) if schemas else "None"

    fields = [
        ("Type", "Svix Event Type"),
        ("Event Name", name),
        ("Status", status_str),
        ("Description", description),
        ("Schema Versions Defined", schema_keys),
    ]
    return {
        "id": f"event_type:{name}",
        "title": f"Svix Event: {name} [{status_str}]",
        "content": "\n".join(_lines(fields)),
        "url": f"{dashboard_url}/event-types/{name}",
        "_deleted": False,
    }


class _SvixStateWalker:
    def __init__(
        self,
        client: Any,
        dashboard_url: str,
        state: dict[str, Any],
        stats: dict[str, int],
        *,
        include: Sequence[str] = VALID_INCLUDES,
        include_archived_events: bool = False,
    ) -> None:
        self.client = client
        self.dashboard_url = dashboard_url.rstrip("/")
        self.state = state
        self.stats = stats
        self.include = tuple(include)
        self.include_archived_events = include_archived_events

        self.fingerprints: dict[str, str] = self.state.setdefault("fingerprints", {})
        self.seen: list[str] = []

    def rows(self) -> Iterator[dict[str, Any]]:
        self.stats.setdefault("scanned", 0)
        self.stats.setdefault("emitted", 0)
        self.stats.setdefault("skipped", 0)
        self.stats.setdefault("deleted", 0)
        self.stats.setdefault("failed", 0)

        # 1. Applications and Endpoints
        apps: list[dict[str, Any]] = []
        if "applications" in self.include or "endpoints" in self.include:
            apps = list(self._walk_applications())
            if "applications" in self.include:
                for app in apps:
                    row = render_application(app, self.dashboard_url)
                    fp = _hash(row)
                    if self._changed(row["id"], fp):
                        yield self._emit(row, fp)

        if "endpoints" in self.include:
            for app in apps:
                app_id = str(app["id"])
                for ep in self._walk_endpoints(app_id):
                    row = render_endpoint(ep, app_id, self.dashboard_url)
                    fp = _hash(row)
                    if self._changed(row["id"], fp):
                        yield self._emit(row, fp)

        # 2. Event Types
        if "event_types" in self.include:
            for ev in self._walk_event_types():
                row = render_event_type(ev, self.dashboard_url)
                fp = _hash(row)
                if self._changed(row["id"], fp):
                    yield self._emit(row, fp)

        # 3. Reconcile deletions
        self._close_cycle()
        yield from self._forget()

    def _walk_applications(self) -> Iterator[dict[str, Any]]:
        iterator: str | None = None
        while True:
            params: dict[str, Any] = {"limit": DEFAULT_PAGE_LIMIT}
            if iterator:
                params["iterator"] = iterator
            res = self.client.request("GET", "api/v1/app", params=params) or {}
            items = res.get("data") or []
            yield from items
            iterator = res.get("iterator")
            done = res.get("done", not bool(iterator))
            if done or not iterator or len(items) == 0:
                break

    def _walk_endpoints(self, app_id: str) -> Iterator[dict[str, Any]]:
        iterator: str | None = None
        while True:
            params: dict[str, Any] = {"limit": DEFAULT_PAGE_LIMIT}
            if iterator:
                params["iterator"] = iterator
            res = (
                self.client.request(
                    "GET",
                    f"api/v1/app/{app_id}/endpoint",
                    params=params,
                )
                or {}
            )
            items = res.get("data") or []
            yield from items
            iterator = res.get("iterator")
            done = res.get("done", not bool(iterator))
            if done or not iterator or len(items) == 0:
                break

    def _walk_event_types(self) -> Iterator[dict[str, Any]]:
        iterator: str | None = None
        while True:
            params: dict[str, Any] = {
                "limit": DEFAULT_PAGE_LIMIT,
                "include_archived": self.include_archived_events,
            }
            if iterator:
                params["iterator"] = iterator
            res = self.client.request("GET", "api/v1/event-type", params=params) or {}
            items = res.get("data") or []
            yield from items
            iterator = res.get("iterator")
            done = res.get("done", not bool(iterator))
            if done or not iterator or len(items) == 0:
                break

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
            except (SvixAPIError, SvixAccessError) as exc:
                self.stats["failed"] += 1
                logger.warning("Svix deletion check failed, will retry next run: %s", exc)
                gone = False

            if gone:
                self.fingerprints.pop(row_id, None)
                self.stats["deleted"] += 1
                yield {"id": row_id, "_deleted": True}
            pending.pop(0)

    def _is_gone(self, row_id: str) -> bool:
        parts = row_id.split(":")
        prefix = parts[0]
        if prefix == "app":
            if "applications" not in self.include:
                return True
            app_id = parts[1]
            try:
                self.client.request("GET", f"api/v1/app/{app_id}")
                return False
            except SvixNotFoundError:
                return True
        elif prefix == "endpoint":
            if "endpoints" not in self.include:
                return True
            app_id, ep_id = parts[1], parts[2]
            try:
                self.client.request("GET", f"api/v1/app/{app_id}/endpoint/{ep_id}")
                return False
            except SvixNotFoundError:
                return True
        elif prefix == "event_type":
            if "event_types" not in self.include:
                return True
            name = parts[1]
            try:
                ev = self.client.request("GET", f"api/v1/event-type/{name}")
                return bool(ev.get("archived") and not self.include_archived_events)
            except SvixNotFoundError:
                return True
        return True


def _iter_rows(
    client: Any,
    dashboard_url: str,
    state: dict[str, Any],
    stats: dict[str, int],
    **kwargs: Any,
) -> Iterator[dict[str, Any]]:
    walker = _SvixStateWalker(client, dashboard_url, state, stats, **kwargs)
    yield from walker.rows()


def svix_source(
    *,
    resource_name: str,
    api_token: str | None = None,
    base_url: str = DEFAULT_SVIX_BASE_URL,
    dashboard_url: str = DEFAULT_DASHBOARD_URL,
    service: Any = None,
    check_active: Callable[[], None] | None = None,
    include: Sequence[str] = VALID_INCLUDES,
    include_archived_events: bool = False,
):
    """Return a dlt resource yielding Svix webhooks and event infrastructure for cognee.remember."""
    if getattr(dlt_utils, "DOCUMENT_SYNC_VERSION", 0) < 1:
        raise RuntimeError("Svix sync requires cognee>=1.6.3 (table-scoped document cleanup).")
    if not isinstance(resource_name, str) or not resource_name.strip():
        raise ValueError("svix_source needs a valid resource_name")

    include = tuple(include)
    if not include or set(include) - set(VALID_INCLUDES):
        raise ValueError(f"include must be a non-empty subset of {VALID_INCLUDES}")

    client = (
        service if service is not None else SvixClient(api_token=api_token or "", base_url=base_url)
    )
    resolved_dash = (dashboard_url or DEFAULT_DASHBOARD_URL).rstrip("/")
    stats: dict[str, int] = {}

    @dlt.resource(
        name=resource_name,
        primary_key="id",
        write_disposition="merge",
        columns={"_deleted": {"data_type": "bool", "hard_delete": True}},
    )
    def svix_workspace():
        stats.clear()
        stats.update(scanned=0, skipped=0, deleted=0, failed=0)
        rows = _iter_rows(
            client=client,
            dashboard_url=resolved_dash,
            state=dlt.current.resource_state(),
            stats=stats,
            include=include,
            include_archived_events=include_archived_events,
        )
        yield from dlt_utils.guarded_rows(rows, check_active)

    res = svix_workspace()
    setattr(res, DOCUMENT_SOURCE_ATTR, "svix")
    setattr(res, dlt_utils.PIPELINE_SCOPE_ATTR, resource_name)
    res.cognee_sync_stats = stats
    return res


svix = svix_source
