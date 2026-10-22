"""Fly.io connector for cognee: a dlt resource that syncs apps, machines, regions, and volumes into memory.

Usage::

    await cognee.remember(
        flyio_source(api_key="FlyV1 ...", resource_name="flyio_prod"),
        dataset_name="infrastructure_memory",
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

logger = get_logger("flyio_connector")

DEFAULT_API_URL = "https://api.machines.dev/v1"
DEFAULT_DASHBOARD_URL = "https://fly.io/dashboard"

KINDS = ("apps", "machines", "volumes")
DEFAULT_MAX_REQUESTS = 500
_TIMEOUT = (10, 30)
_RETRIES = 3

_SECRET_PATTERNS = [
    re.compile(r"(FlyV1\s+[A-Za-z0-9_\-\.]{8,})", re.IGNORECASE),
    re.compile(r"(fo1_[A-Za-z0-9_\-]{8,})"),
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


class FlyioSourceError(RuntimeError):
    """Base exception for Fly.io connector errors."""


class FlyioAPIError(FlyioSourceError):
    """API communication failure or unexpected response."""


class FlyioAuthError(FlyioSourceError):
    """Fly.io rejected the API key (HTTP 401)."""


class FlyioAccessError(FlyioSourceError):
    """Access forbidden for the requested resource (HTTP 403)."""


class FlyioRateLimitedError(FlyioSourceError):
    """Rate limit exceeded (HTTP 429)."""


class FlyioNotFoundError(FlyioAPIError):
    """Resource not found (HTTP 404)."""


class _CutShortError(Exception):
    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


class FlyioClient:
    """HTTP client for Fly.io Machines REST API v1."""

    def __init__(
        self,
        api_key: str,
        api_url: str = DEFAULT_API_URL,
        dashboard_url: str = DEFAULT_DASHBOARD_URL,
        org_slug: str | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ):
        key = (api_key or "").strip()
        if not key:
            raise ValueError("The Fly.io API key is empty or has an invalid format")
        self._api_key = key
        self.api_url = api_url.rstrip("/")
        self.dashboard_url = dashboard_url.rstrip("/")
        self.org_slug = org_slug
        self._sleep = sleep
        self.rate_limit: dict[str, int | None] = {}

    def __repr__(self) -> str:
        return f"FlyioClient(api_url='{self.api_url}', key=<redacted>)"

    def request(
        self,
        method: str,
        path: str,
        *,
        body: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
    ) -> Any:
        url = f"{self.api_url}{path}" if path.startswith("/") else f"{self.api_url}/{path}"
        auth_header = self._api_key if self._api_key.lower().startswith("flyv1 ") else f"Bearer {self._api_key}"
        headers = {
            "Authorization": auth_header,
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
                    raise FlyioAPIError(f"Fly.io request failed: {type(exc).__name__}") from None
                self._sleep(2**attempt)
                continue

            if response.status_code >= 500 and not last:
                self._sleep(2**attempt)
                continue

            return self._parse(response, path)

    def _parse(self, response: requests.Response, path: str) -> Any:
        status = response.status_code
        if status == 401:
            raise FlyioAuthError("Fly.io rejected the API key (HTTP 401)")
        if status == 403:
            raise FlyioAccessError(f"Fly.io access forbidden for {path} (HTTP 403)")
        if status == 429:
            retry_after = response.headers.get("Retry-After")
            msg = f"Fly.io rate limit exceeded (HTTP 429), retry after {retry_after}s" if retry_after else "Fly.io rate limit exceeded (HTTP 429)"
            raise FlyioRateLimitedError(msg)
        if status == 404:
            raise FlyioNotFoundError(f"Fly.io resource not found (HTTP 404): {path}")

        try:
            payload = response.json()
        except ValueError:
            payload = None

        if status not in (200, 201):
            raise FlyioAPIError(f"Fly.io API request failed with HTTP {status}: {response.text[:200]}")

        return payload


def render_app(app: dict[str, Any], dashboard_url: str = DEFAULT_DASHBOARD_URL) -> dict[str, Any]:
    """Render a Fly.io app into a document."""
    app_id = str(app.get("id") or app.get("name") or "unnamed_app")
    name = app.get("name") or app_id
    status = app.get("status") or "deployed"
    org = app.get("organization", {}).get("slug") if isinstance(app.get("organization"), dict) else app.get("organization") or "personal"
    title = f"Fly.io App: {name} [{status}]"

    fields = [
        f"App Name: {name}",
        f"App ID: {app_id}",
        f"Organization: {org}",
        f"Status: {status}",
        f"Hostname: {name}.fly.dev",
    ]
    current_release = app.get("current_release")
    if current_release and isinstance(current_release, dict):
        fields.append(f"Current Release Version: v{current_release.get('version', '')}")
        fields.append(f"Release Description: {current_release.get('description', '')}")

    return {
        "id": f"app:{name}",
        "title": title,
        "content": sanitize_text("\n".join(fields)),
        "url": f"{dashboard_url}/apps/{name}",
        "_deleted": False,
    }


def render_machine(app_name: str, mach: dict[str, Any], dashboard_url: str = DEFAULT_DASHBOARD_URL) -> dict[str, Any]:
    """Render a Fly.io machine into a document."""
    mach_id = str(mach.get("id") or "unnamed_machine")
    name = mach.get("name") or mach_id
    state = mach.get("state") or "started"
    region = mach.get("region") or "global"
    title = f"Fly.io Machine: {app_name} / {name} ({mach_id}) [{state}]"

    config = mach.get("config") or {}
    image = config.get("image") or "N/A"
    guest = config.get("guest") or {}
    cpus = guest.get("cpus") or 1
    memory_mb = guest.get("memory_mb") or 256

    checks_info = []
    for chk in mach.get("checks") or []:
        if isinstance(chk, dict):
            checks_info.append(f"{chk.get('name', 'check')}: {chk.get('status', 'unknown')}")

    fields = [
        f"App: {app_name}",
        f"Machine ID: {mach_id}",
        f"Name: {name}",
        f"State: {state}",
        f"Region: {region}",
        f"Image: {image}",
        f"Sizing: {cpus} CPU, {memory_mb} MB RAM",
        f"Health Checks: {', '.join(checks_info) if checks_info else 'None'}",
        f"Created At: {mach.get('created_at') or 'N/A'}",
        f"Updated At: {mach.get('updated_at') or 'N/A'}",
    ]

    return {
        "id": f"machine:{app_name}:{mach_id}",
        "title": title,
        "content": sanitize_text("\n".join(fields)),
        "url": f"{dashboard_url}/apps/{app_name}/machines/{mach_id}",
        "_deleted": False,
    }


def render_volume(app_name: str, vol: dict[str, Any], dashboard_url: str = DEFAULT_DASHBOARD_URL) -> dict[str, Any]:
    """Render a Fly.io volume into a document."""
    vol_id = str(vol.get("id") or "unnamed_volume")
    name = vol.get("name") or vol_id
    region = vol.get("region") or "global"
    size_gb = vol.get("size_gb") or vol.get("size") or "N/A"
    title = f"Fly.io Volume: {app_name} / {name} ({vol_id})"

    fields = [
        f"App: {app_name}",
        f"Volume ID: {vol_id}",
        f"Name: {name}",
        f"Region: {region}",
        f"Size (GB): {size_gb}",
        f"State: {vol.get('state') or 'created'}",
        f"Encrypted: {vol.get('encrypted', True)}",
        f"Attached Machine: {vol.get('attached_machine_id') or 'None'}",
    ]

    return {
        "id": f"volume:{app_name}:{vol_id}",
        "title": title,
        "content": sanitize_text("\n".join(fields)),
        "url": f"{dashboard_url}/apps/{app_name}/volumes/{vol_id}",
        "_deleted": False,
    }


def _hash(val: Any) -> str:
    serialized = json.dumps(val, sort_keys=True, default=str)
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()[:16]


class _Walker:
    def __init__(
        self,
        client: FlyioClient,
        state: dict[str, Any],
        stats: dict[str, int],
        *,
        apps: list[str] | None = None,
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
        self.apps = apps
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

        # Step 1: List apps
        params = {}
        if self.client.org_slug:
            params["org_slug"] = self.client.org_slug

        try:
            data = self._call("GET", "/v1/apps", params=params)
            all_apps = data.get("apps", []) if isinstance(data, dict) else (data if isinstance(data, list) else [])
        except _CutShortError:
            raise
        except Exception as exc:
            logger.warning("Failed to fetch Fly.io apps: %s", exc)
            all_apps = []

        target_apps = [a for a in all_apps if (not self.apps or a.get("name") in self.apps)]

        if "apps" in self.include:
            for app in target_apps:
                row = render_app(app, getattr(self.client, "dashboard_url", DEFAULT_DASHBOARD_URL))
                fp = _hash(row)
                if self._changed(row["id"], fp):
                    yield self._emit(row, fp)

        for app in target_apps:
            app_name = app.get("name")
            if not app_name:
                continue

            if "machines" in self.include:
                try:
                    m_data = self._call("GET", f"/v1/apps/{app_name}/machines")
                    machines = m_data if isinstance(m_data, list) else m_data.get("machines", [])
                except _CutShortError:
                    raise
                except Exception as exc:
                    logger.warning("Failed to fetch Fly.io machines for app %s: %s", app_name, exc)
                    machines = []

                for mach in machines:
                    row = render_machine(app_name, mach, getattr(self.client, "dashboard_url", DEFAULT_DASHBOARD_URL))
                    fp = _hash(row)
                    if self._changed(row["id"], fp):
                        yield self._emit(row, fp)

            if "volumes" in self.include:
                try:
                    v_data = self._call("GET", f"/v1/apps/{app_name}/volumes")
                    volumes = v_data if isinstance(v_data, list) else v_data.get("volumes", [])
                except _CutShortError:
                    raise
                except Exception as exc:
                    logger.warning("Failed to fetch Fly.io volumes for app %s: %s", app_name, exc)
                    volumes = []

                for vol in volumes:
                    row = render_volume(app_name, vol, getattr(self.client, "dashboard_url", DEFAULT_DASHBOARD_URL))
                    fp = _hash(row)
                    if self._changed(row["id"], fp):
                        yield self._emit(row, fp)

        self._close_cycle()
        yield from self._forget()

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
    client: FlyioClient,
    state: dict[str, Any],
    stats: dict[str, int],
    *,
    apps: list[str] | None = None,
    include: tuple[str, ...],
    max_requests: int = DEFAULT_MAX_REQUESTS,
    check_active: Callable[[], None] | None = None,
) -> Iterator[dict[str, Any]]:
    walker = _Walker(
        client,
        state,
        stats,
        apps=apps,
        include=include,
        max_requests=max_requests,
        check_active=check_active,
    )
    try:
        yield from walker.rows()
    except _CutShortError as cut:
        stats["failed"] = max(1, stats.get("failed", 0))
        stats[f"failed_{cut.reason}"] = 1
        logger.warning("Fly.io sync stopped early (%s); it will resume on next run.", cut.reason)


def flyio_source(
    *,
    resource_name: str,
    api_key: str | None = None,
    api_url: str = DEFAULT_API_URL,
    dashboard_url: str = DEFAULT_DASHBOARD_URL,
    org_slug: str | None = None,
    apps: list[str] | None = None,
    service: Any = None,
    check_active: Callable[[], None] | None = None,
    include: tuple[str, ...] = KINDS,
    max_requests: int = DEFAULT_MAX_REQUESTS,
):
    """Return a dlt resource yielding Fly.io apps, machines, and volumes for ``cognee.remember``.

    Args:
        resource_name: Unique identifier for this connection instance.
        api_key: Fly.io API Token (`FlyV1 ...` or standard API token).
        api_url: Custom Fly.io Machines API base URL.
        dashboard_url: Web dashboard URL for document references.
        org_slug: Optional organization slug to filter apps.
        apps: Optional list of specific app names to ingest.
        service: Injected client instance for testing.
        check_active: Optional authorization checkpoint callback.
        include: Kinds of records to sync: ("apps", "machines", "volumes").
        max_requests: Per-run request budget cap.

    Returns:
        A dlt resource configured with ``primary_key="id"``, ``write_disposition="merge"``,
        and ``_deleted`` hard-delete column.
    """
    import dlt

    if getattr(dlt_utils, "DOCUMENT_SYNC_VERSION", 0) < 1:
        raise RuntimeError("Fly.io connector requires cognee>=1.6.3 (table-scoped document cleanup).")
    if not isinstance(resource_name, str) or not resource_name.strip():
        raise ValueError("flyio_source requires a non-empty resource_name")
    if service is None and not api_key:
        raise ValueError("flyio_source requires an api_key or a service instance")

    include = tuple(include)
    if not include or set(include) - set(KINDS):
        raise ValueError(f"include must be a non-empty subset of {KINDS}")

    client = service if service is not None else FlyioClient(
        api_key or "",
        api_url=api_url,
        dashboard_url=dashboard_url,
        org_slug=org_slug,
    )
    stats: dict[str, int] = {}

    @dlt.resource(
        name=resource_name,
        primary_key="id",
        write_disposition="merge",
        columns={"_deleted": {"data_type": "bool", "hard_delete": True}},
    )
    def flyio_workplace():
        stats.clear()
        stats.update(scanned=0, skipped=0, deleted=0, failed=0)
        rows = _iter_rows(
            client,
            dlt.current.resource_state(),
            stats,
            apps=apps,
            include=include,
            max_requests=max_requests,
            check_active=check_active,
        )
        yield from dlt_utils.guarded_rows(rows, check_active)

    resource = flyio_workplace()
    setattr(resource, DOCUMENT_SOURCE_ATTR, "flyio")
    setattr(resource, dlt_utils.PIPELINE_SCOPE_ATTR, resource_name)
    resource.cognee_sync_stats = stats
    return resource
