"""Doppler connector for cognee: a dlt resource that syncs project environments, config metadata, and activity audit logs into memory.

CRITICAL SECURITY GUARANTEE:
    This connector only ingests project structures, environment configurations, variable names/keys,
    and activity audit logs. Secret values are NEVER fetched, read, or stored into memory.

Usage::

    await cognee.remember(
        doppler_source(api_key="<doppler service or personal token>", resource_name="doppler_main"),
        dataset_name="devops_memory",
        primary_key="id",
        write_disposition="merge",
    )
"""

import hashlib
import json
import time
from collections.abc import Callable, Iterator
from typing import Any

import requests
from cognee.shared.logging_utils import get_logger
from cognee.tasks.ingestion import dlt_utils
from cognee.tasks.ingestion.dlt_utils import DOCUMENT_SOURCE_ATTR

logger = get_logger("doppler_connector")

DEFAULT_API_URL = "https://api.doppler.com/v3"
DEFAULT_DASHBOARD_URL = "https://dashboard.doppler.com"

KINDS = ("projects", "environments", "configs", "logs")
DEFAULT_MAX_REQUESTS = 500
_TIMEOUT = (10, 30)
_RETRIES = 3


class DopplerSourceError(RuntimeError):
    """Base exception for Doppler source errors."""


class DopplerAPIError(DopplerSourceError):
    """API communication failure or unexpected response."""


class DopplerAuthError(DopplerSourceError):
    """Doppler rejected the API token (HTTP 401)."""


class DopplerAccessError(DopplerSourceError):
    """The API token does not have access to the requested project/workplace (HTTP 403)."""


class DopplerRateLimitedError(DopplerSourceError):
    """Rate limit exceeded (HTTP 429)."""


class DopplerNotFoundError(DopplerAPIError):
    """Requested resource was not found (HTTP 404)."""


class _CutShortError(Exception):
    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


class DopplerClient:
    """HTTP client for the Doppler REST API v3."""

    def __init__(
        self,
        api_key: str,
        api_url: str = DEFAULT_API_URL,
        dashboard_url: str = DEFAULT_DASHBOARD_URL,
        sleep: Callable[[float], None] = time.sleep,
    ):
        key = (api_key or "").strip()
        if not key:
            raise ValueError("The Doppler API token is empty or has an invalid format")
        self._api_key = key
        self.api_url = api_url.rstrip("/")
        self.dashboard_url = dashboard_url.rstrip("/")
        self._sleep = sleep
        self.rate_limit: dict[str, int | None] = {}

    def __repr__(self) -> str:
        return f"DopplerClient(api_url='{self.api_url}', token=<redacted>)"

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
                    raise DopplerAPIError(f"Doppler request failed: {type(exc).__name__}") from None
                self._sleep(2**attempt)
                continue

            if response.status_code >= 500 and not last:
                self._sleep(2**attempt)
                continue

            return self._parse(response, path)

    def _parse(self, response: requests.Response, path: str) -> Any:
        status = response.status_code
        if status == 401:
            raise DopplerAuthError("Doppler rejected the API token (HTTP 401)")
        if status == 403:
            raise DopplerAccessError(f"Doppler access forbidden for {path} (HTTP 403)")
        if status == 429:
            retry_after = response.headers.get("Retry-After")
            msg = f"Doppler rate limit exceeded (HTTP 429), retry after {retry_after}s" if retry_after else "Doppler rate limit exceeded (HTTP 429)"
            raise DopplerRateLimitedError(msg)
        if status == 404:
            raise DopplerNotFoundError(f"Doppler resource not found (HTTP 404): {path}")

        try:
            payload = response.json()
        except ValueError:
            payload = None

        if status not in (200, 201):
            raise DopplerAPIError(f"Doppler API request failed with HTTP {status}: {response.text[:200]}")

        return payload


def render_project(project: dict[str, Any], dashboard_url: str = DEFAULT_DASHBOARD_URL) -> dict[str, Any]:
    """Render a Doppler project metadata into a document."""
    name = project.get("name") or project.get("slug") or project.get("id") or "unnamed_project"
    proj_id = name
    title = f"Project: {name}"

    fields = [
        f"Project Name: {name}",
        f"Project ID: {proj_id}",
        f"Description: {project.get('description') or 'N/A'}",
        f"Created At: {project.get('created_at') or 'N/A'}",
    ]

    return {
        "id": f"project:{proj_id}",
        "title": title,
        "content": "\n".join(fields),
        "url": f"{dashboard_url}/workplace/projects/{proj_id}",
        "_deleted": False,
    }


def render_environment(env: dict[str, Any], project_name: str, dashboard_url: str = DEFAULT_DASHBOARD_URL) -> dict[str, Any]:
    """Render an environment metadata into a document."""
    slug = env.get("slug") or env.get("id") or "env"
    name = env.get("name") or slug
    title = f"Environment: {project_name} / {name} ({slug})"

    fields = [
        f"Project: {project_name}",
        f"Environment Name: {name}",
        f"Environment Slug: {slug}",
        f"Created At: {env.get('created_at') or 'N/A'}",
    ]

    return {
        "id": f"environment:{project_name}:{slug}",
        "title": title,
        "content": "\n".join(fields),
        "url": f"{dashboard_url}/workplace/projects/{project_name}/environments/{slug}",
        "_deleted": False,
    }


def render_config(config: dict[str, Any], variable_keys: list[str], dashboard_url: str = DEFAULT_DASHBOARD_URL) -> dict[str, Any]:
    """Render a configuration metadata document (variable names only, never secret values)."""
    cfg_name = config.get("name") or "config"
    project = config.get("project") or "default"
    environment = config.get("environment") or "default"
    title = f"Config: {project} / {cfg_name} [{environment}]"

    fields = [
        f"Config Name: {cfg_name}",
        f"Project: {project}",
        f"Environment: {environment}",
        f"Root Config: {config.get('root', False)}",
        f"Locked: {config.get('locked', False)}",
        f"Configured Variable Keys Count: {len(variable_keys)}",
    ]

    if variable_keys:
        keys_sorted = sorted(variable_keys)
        fields.append(f"Configured Variables (Keys Only): {', '.join(keys_sorted)}")

    return {
        "id": f"config:{project}:{cfg_name}",
        "title": title,
        "content": "\n".join(fields),
        "url": f"{dashboard_url}/workplace/projects/{project}/configs/{cfg_name}",
        "_deleted": False,
    }


def render_log(log: dict[str, Any], project_name: str, dashboard_url: str = DEFAULT_DASHBOARD_URL) -> dict[str, Any]:
    """Render an activity audit log into a document."""
    log_id = log.get("id") or str(log.get("created_at") or "")
    text = log.get("text") or "Config change"
    user_info = log.get("user") or {}
    user_name = user_info.get("name") or user_info.get("email") or "System / Service Token"
    title = f"Audit Log: {project_name} - {text[:60]}"

    fields = [
        f"Project: {project_name}",
        f"Log ID: {log_id}",
        f"Action Description: {text}",
        f"Actor: {user_name}",
        f"Config: {log.get('config') or 'N/A'}",
        f"Timestamp: {log.get('created_at') or 'N/A'}",
    ]

    return {
        "id": f"log:{project_name}:{log_id}",
        "title": title,
        "content": "\n".join(fields),
        "url": f"{dashboard_url}/workplace/projects/{project_name}/activity",
        "_deleted": False,
    }


def _hash(val: Any) -> str:
    """Compute deterministic SHA-256 fingerprint for document content."""
    serialized = json.dumps(val, sort_keys=True, default=str)
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()[:16]


class _Walker:
    def __init__(
        self,
        client: DopplerClient,
        state: dict[str, Any],
        stats: dict[str, int],
        *,
        include: tuple[str, ...],
        projects: list[str] | None = None,
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
        self.selected_projects = projects
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

        # Fetch projects
        try:
            data = self._call("GET", "/v3/projects")
            projects_list = data.get("projects") or []
        except Exception as exc:
            logger.warning("Failed to fetch Doppler projects: %s", exc)
            projects_list = []

        active_projects = []
        for p in projects_list:
            p_name = p.get("name") or p.get("slug") or p.get("id")
            if self.selected_projects and p_name not in self.selected_projects:
                continue
            active_projects.append(p)

            if "projects" in self.include:
                row = render_project(p, getattr(self.client, "dashboard_url", DEFAULT_DASHBOARD_URL))
                fp = _hash(row)
                if self._changed(row["id"], fp):
                    yield self._emit(row, fp)

        # For each active project, fetch environments, configs, and audit logs
        for p in active_projects:
            p_name = p.get("name") or p.get("slug") or p.get("id")

            if "environments" in self.include:
                yield from self._environment_rows(p_name)

            if "configs" in self.include:
                yield from self._config_rows(p_name)

            if "logs" in self.include:
                yield from self._log_rows(p_name)

        self._close_cycle()
        yield from self._forget()

    def _environment_rows(self, project_name: str) -> Iterator[dict[str, Any]]:
        try:
            data = self._call("GET", "/v3/environments", params={"project": project_name})
            environments = data.get("environments") or []
        except Exception as exc:
            logger.warning("Failed to fetch environments for project %s: %s", project_name, exc)
            environments = []

        for env in environments:
            row = render_environment(env, project_name, getattr(self.client, "dashboard_url", DEFAULT_DASHBOARD_URL))
            fp = _hash(row)
            if self._changed(row["id"], fp):
                yield self._emit(row, fp)

    def _config_rows(self, project_name: str) -> Iterator[dict[str, Any]]:
        try:
            data = self._call("GET", "/v3/configs", params={"project": project_name})
            configs = data.get("configs") or []
        except Exception as exc:
            logger.warning("Failed to fetch configs for project %s: %s", project_name, exc)
            configs = []

        for cfg in configs:
            cfg_name = cfg.get("name") or ""
            # Fetch secret keys metadata (Keys only)
            variable_keys = []
            try:
                sec_data = self._call(
                    "GET",
                    "/v3/configs/config/secrets",
                    params={"project": project_name, "config": cfg_name, "include_dynamic_secrets": "false"},
                )
                secrets_dict = sec_data.get("secrets") or {}
                # Strictly extract the keys only!
                variable_keys = list(secrets_dict.keys())
            except Exception as exc:
                logger.debug("Could not fetch secrets metadata for config %s: %s", cfg_name, exc)

            row = render_config(cfg, variable_keys, getattr(self.client, "dashboard_url", DEFAULT_DASHBOARD_URL))
            fp = _hash(row)
            if self._changed(row["id"], fp):
                yield self._emit(row, fp)

    def _log_rows(self, project_name: str) -> Iterator[dict[str, Any]]:
        try:
            data = self._call("GET", "/v3/logs", params={"project": project_name})
            logs = data.get("logs") or []
        except Exception as exc:
            logger.warning("Failed to fetch audit logs for project %s: %s", project_name, exc)
            logs = []

        for log_entry in logs:
            row = render_log(log_entry, project_name, getattr(self.client, "dashboard_url", DEFAULT_DASHBOARD_URL))
            fp = _hash(row)
            if self._changed(row["id"], fp):
                yield self._emit(row, fp)

    def _close_cycle(self) -> None:
        missing = set(self.fingerprints) - set(self.seen)
        # Deletions apply to environments, configs, and projects (logs are audit records and retained)
        tombstoneable = [mid for mid in missing if mid.startswith(("project:", "environment:", "config:"))]
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
    client: DopplerClient,
    state: dict[str, Any],
    stats: dict[str, int],
    *,
    include: tuple[str, ...],
    projects: list[str] | None = None,
    max_requests: int = DEFAULT_MAX_REQUESTS,
    check_active: Callable[[], None] | None = None,
) -> Iterator[dict[str, Any]]:
    walker = _Walker(
        client,
        state,
        stats,
        include=include,
        projects=projects,
        max_requests=max_requests,
        check_active=check_active,
    )
    try:
        yield from walker.rows()
    except _CutShortError as cut:
        stats["failed"] = max(1, stats.get("failed", 0))
        stats[f"failed_{cut.reason}"] = 1
        logger.warning("Doppler sync stopped early (%s); it will resume on next run.", cut.reason)


def doppler_source(
    *,
    resource_name: str,
    api_key: str | None = None,
    api_url: str = DEFAULT_API_URL,
    dashboard_url: str = DEFAULT_DASHBOARD_URL,
    service: Any = None,
    check_active: Callable[[], None] | None = None,
    projects: list[str] | None = None,
    include: tuple[str, ...] = KINDS,
    max_requests: int = DEFAULT_MAX_REQUESTS,
):
    """Return a dlt resource yielding Doppler project configs, environments, and audit logs for ``cognee.remember``.

    Args:
        resource_name: Unique name for this connection instance.
        api_key: Doppler Service Token (`dp.st....`) or Personal Token.
        api_url: Doppler API URL (default "https://api.doppler.com/v3").
        dashboard_url: Web dashboard URL for document references.
        service: Injected client instance for testing.
        check_active: Optional authorization checkpoint callback.
        projects: Optional list of project names to scope the sync.
        include: Kinds of records to sync: ("projects", "environments", "configs", "logs").
        max_requests: Maximum requests per run budget.

    Returns:
        A dlt resource configured with ``primary_key="id"``, ``write_disposition="merge"``,
        and ``_deleted`` hard-delete column.
    """
    import dlt

    if getattr(dlt_utils, "DOCUMENT_SYNC_VERSION", 0) < 1:
        raise RuntimeError("Doppler connector requires cognee>=1.6.3 (table-scoped document cleanup).")
    if not isinstance(resource_name, str) or not resource_name.strip():
        raise ValueError("doppler_source requires a non-empty resource_name")
    if service is None and not api_key:
        raise ValueError("doppler_source requires an api_key or a service instance")

    include = tuple(include)
    if not include or set(include) - set(KINDS):
        raise ValueError(f"include must be a non-empty subset of {KINDS}")

    client = service if service is not None else DopplerClient(api_key or "", api_url=api_url, dashboard_url=dashboard_url)
    stats: dict[str, int] = {}

    @dlt.resource(
        name=resource_name,
        primary_key="id",
        write_disposition="merge",
        columns={"_deleted": {"data_type": "bool", "hard_delete": True}},
    )
    def doppler_workplace():
        stats.clear()
        stats.update(scanned=0, skipped=0, deleted=0, failed=0)
        rows = _iter_rows(
            client,
            dlt.current.resource_state(),
            stats,
            include=include,
            projects=projects,
            max_requests=max_requests,
            check_active=check_active,
        )
        yield from dlt_utils.guarded_rows(rows, check_active)

    resource = doppler_workplace()
    setattr(resource, DOCUMENT_SOURCE_ATTR, "doppler")
    setattr(resource, dlt_utils.PIPELINE_SCOPE_ATTR, resource_name)
    resource.cognee_sync_stats = stats
    return resource
