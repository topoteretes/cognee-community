"""Prefect workflow orchestration data-source connector for cognee.

A dlt resource that syncs your Prefect flows, deployments, and flow runs into memory,
enabling knowledge graph search and semantic recall over orchestration metadata:

    await cognee.remember(
        prefect_source(
            resource_name="prefect_prod",
            base_url="https://api.prefect.cloud/api/accounts/.../workspaces/...",
            api_key="<prefect api key>",
        ),
        dataset_name="prefect",
        primary_key="id",
        write_disposition="merge",
    )
"""

import hashlib
import json
import re
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlparse

import requests
from cognee.shared.logging_utils import get_logger
from cognee.tasks.ingestion import dlt_utils
from cognee.tasks.ingestion.dlt_utils import DOCUMENT_SOURCE_ATTR

logger = get_logger("prefect_connector")

DEFAULT_BASE_URL = "http://localhost:4200/api"
KINDS = ("flows", "deployments", "flow_runs")
PAGE_SIZE = 100
DEFAULT_MAX_REQUESTS = 500
_TIMEOUT = (10, 30)
_RETRIES = 3


class PrefectSourceError(RuntimeError):
    """Base class for errors raised by this source."""


class PrefectAPIError(PrefectSourceError):
    """Prefect API answered with an error or could not be reached."""


class PrefectAuthError(PrefectSourceError):
    """Prefect rejected authentication (HTTP 401)."""


class PrefectAccessError(PrefectSourceError):
    """Prefect refused access to a resource or workspace (HTTP 403)."""


class PrefectRateLimitedError(PrefectSourceError):
    """Prefect answered with HTTP 429 rate limit exceeded."""


class PrefectNotFoundError(PrefectAPIError):
    """The requested Prefect object was not found (HTTP 404)."""


def _int_header(headers: Any, name: str) -> int | None:
    try:
        return int(headers.get(name))
    except (TypeError, ValueError):
        return None


def _clean_endpoint(path: str) -> str:
    """Path with record UUID / ID stripped for clean error messages."""
    parts = path.strip("/").split("/")
    if len(parts) >= 2 and parts[-1] not in ("filter", "count", "history"):
        return f"/{parts[0]}/{{id}}"
    return path


def _infer_ui_url(base_url: str) -> str:
    """Infer the corresponding web UI root URL from a Prefect API base URL."""
    cleaned = base_url.rstrip("/")
    # Prefect Cloud pattern: https://api.prefect.cloud/api/accounts/{acc}/workspaces/{ws}
    cloud_match = re.search(
        r"https?://api\.prefect\.cloud/api/accounts/([^/]+)/workspaces/([^/]+)",
        cleaned,
    )
    if cloud_match:
        acc, ws = cloud_match.groups()
        return f"https://app.prefect.cloud/account/{acc}/workspace/{ws}"

    # Self-hosted / OSS pattern: http://localhost:4200/api -> http://localhost:4200
    if cleaned.endswith("/api"):
        return cleaned[:-4]
    return cleaned


class PrefectClient:
    """Minimal synchronous REST client for the Prefect REST API."""

    def __init__(
        self,
        base_url: str = DEFAULT_BASE_URL,
        api_key: str | None = None,
        *,
        sleep: Callable[[float], None] = time.sleep,
    ):
        base = (base_url or "").strip().rstrip("/")
        if not base:
            raise ValueError("The Prefect base_url cannot be empty")
        parsed = urlparse(base)
        if not parsed.scheme or not parsed.netloc:
            raise ValueError(f"Invalid Prefect base_url: {base_url!r}")

        key = (api_key or "").strip()
        if key and (not key.isascii() or not key.isprintable() or " " in key):
            raise ValueError("The Prefect API key has an invalid format")

        self.base_url = base
        self._api_key = key
        self._sleep = sleep
        self.rate_limit: dict[str, int | None] = {}

    def __repr__(self) -> str:
        key_repr = "<redacted>" if self._api_key else "<none>"
        return f"PrefectClient(base_url={self.base_url!r}, api_key={key_repr})"

    def request(
        self,
        method: str,
        path: str,
        *,
        body: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
    ) -> Any:
        url = f"{self.base_url}/{path.lstrip('/')}"
        headers = {
            "Content-Type": "application/json",
            "Cache-Control": "no-cache",
        }
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"

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
                    raise PrefectAPIError(f"Prefect request failed: {type(exc).__name__}") from None
                self._sleep(2**attempt)
                continue

            if response.status_code >= 500 and not last:
                self._sleep(2**attempt)
                continue

            return self._parse(response, path)

    def _parse(self, response: Any, path: str) -> Any:
        status = response.status_code
        if status == 401:
            raise PrefectAuthError("Prefect rejected authentication (HTTP 401)")
        if status == 403:
            raise PrefectAccessError(f"Prefect denied access to {_clean_endpoint(path)} (HTTP 403)")
        if status == 404:
            raise PrefectNotFoundError(f"Prefect record not found (HTTP 404): {path}")
        if status == 429:
            raise PrefectRateLimitedError("Prefect rate limit exceeded (HTTP 429)")

        try:
            payload = response.json()
        except ValueError:
            payload = None

        if status not in (200, 201):
            raise PrefectAPIError(f"Prefect request failed: HTTP {status}")

        if not isinstance(payload, (list, dict)):
            raise PrefectAPIError(
                f"Prefect sent an unexpected response format for {_clean_endpoint(path)}"
            )

        headers = response.headers
        self.rate_limit = {
            "remaining": _int_header(headers, "x-ratelimit-remaining"),
            "reset": _int_header(headers, "x-ratelimit-reset"),
        }
        return payload


# ---------------------------------------------------------------------------
# Rendering: deterministic representations with strictly redacted parameters
# ---------------------------------------------------------------------------
@dataclass
class PrefectLookups:
    """Lookup table caching flow and deployment names to enrich related records."""

    flow_names: dict[str, str] = field(default_factory=dict)
    deployment_names: dict[str, str] = field(default_factory=dict)


def _text(val: Any) -> str:
    if isinstance(val, list):
        return ", ".join(sorted(_text(x) for x in val if _text(x)))
    return "" if val is None else str(val).strip()


def _lines(pairs: list[tuple[str, str]]) -> list[str]:
    return [f"{label}: {value}" for label, value in pairs if value]


def _format_schedule(schedules_or_schedule: Any) -> str:
    if not schedules_or_schedule:
        return "None"
    if isinstance(schedules_or_schedule, list) and schedules_or_schedule:
        entry = schedules_or_schedule[0]
        schedule_obj = entry.get("schedule") if isinstance(entry, dict) else entry
    else:
        schedule_obj = schedules_or_schedule

    if isinstance(schedule_obj, dict):
        if "cron" in schedule_obj:
            return f"Cron: {schedule_obj.get('cron')}"
        if "interval" in schedule_obj:
            return f"Interval: {schedule_obj.get('interval')}s"
        if "rrule" in schedule_obj:
            return f"RRule: {schedule_obj.get('rrule')}"
    return "Configured"


def _parameter_names(params: Any) -> str:
    """Format parameter keys only, strictly withholding secret values."""
    if not isinstance(params, dict) or not params:
        return "None"
    return ", ".join(sorted(str(k) for k in params))


def render_flow(flow: dict[str, Any], ui_url: str) -> dict[str, Any]:
    flow_id = str(flow["id"])
    name = _text(flow.get("name")) or "Unnamed Flow"
    tags = _text(flow.get("tags"))
    description = _text(flow.get("description"))

    fields = [
        ("Type", "Flow"),
        ("Name", name),
        ("Tags", tags),
        ("Description", description),
        ("Created", _text(flow.get("created"))),
        ("Updated", _text(flow.get("updated"))),
    ]
    return {
        "id": f"flow:{flow_id}",
        "title": f"Flow: {name}",
        "content": "\n".join(_lines(fields)),
        "url": f"{ui_url}/flows/flow/{flow_id}",
        "_deleted": False,
    }


def render_deployment(
    deployment: dict[str, Any], lookups: PrefectLookups, ui_url: str
) -> dict[str, Any]:
    dep_id = str(deployment["id"])
    dep_name = _text(deployment.get("name")) or "Unnamed Deployment"
    flow_id = str(deployment.get("flow_id") or "")
    flow_name = lookups.flow_names.get(flow_id, "Unknown Flow")
    status = _text(deployment.get("status")) or (
        "active" if deployment.get("is_schedule_active") else "paused"
    )

    schedules = deployment.get("schedules") or deployment.get("schedule")
    schedule_text = _format_schedule(schedules)
    param_keys = _parameter_names(deployment.get("parameters"))

    fields = [
        ("Type", "Deployment"),
        ("Name", dep_name),
        ("Flow", flow_name),
        ("Status", status),
        ("Schedule", schedule_text),
        ("Work Pool", _text(deployment.get("work_pool_name"))),
        ("Work Queue", _text(deployment.get("work_queue_name"))),
        ("Entrypoint", _text(deployment.get("entrypoint"))),
        ("Tags", _text(deployment.get("tags"))),
        ("Parameters (keys only)", param_keys),
        ("Created", _text(deployment.get("created"))),
        ("Updated", _text(deployment.get("updated"))),
    ]
    return {
        "id": f"deployment:{dep_id}",
        "title": f"Deployment: {dep_name} ({flow_name})",
        "content": "\n".join(_lines(fields)),
        "url": f"{ui_url}/deployments/deployment/{dep_id}",
        "_deleted": False,
    }


def render_flow_run(run: dict[str, Any], lookups: PrefectLookups, ui_url: str) -> dict[str, Any]:
    run_id = str(run["id"])
    run_name = _text(run.get("name")) or "Unnamed Run"
    flow_id = str(run.get("flow_id") or "")
    flow_name = lookups.flow_names.get(flow_id, "Unknown Flow")

    dep_id = str(run.get("deployment_id") or "")
    dep_name = lookups.deployment_names.get(dep_id, "Ad-hoc / Interactive")

    state_name = _text(run.get("state_name")) or _text(run.get("state_type")) or "UNKNOWN"
    duration = run.get("total_run_time")
    duration_str = f"{duration:.2f}s" if isinstance(duration, (int, float)) else _text(duration)

    param_keys = _parameter_names(run.get("parameters"))

    fields = [
        ("Type", "Flow Run"),
        ("Run Name", run_name),
        ("Flow", flow_name),
        ("Deployment", dep_name),
        ("State", state_name),
        ("State Type", _text(run.get("state_type"))),
        ("Start Time", _text(run.get("start_time"))),
        ("End Time", _text(run.get("end_time"))),
        ("Duration", duration_str),
        ("Run Count", _text(run.get("run_count"))),
        ("Tags", _text(run.get("tags"))),
        ("Parameters (keys only)", param_keys),
    ]
    return {
        "id": f"flow_run:{run_id}",
        "title": f"Flow Run: {run_name} [{state_name}]",
        "content": "\n".join(_lines(fields)),
        "url": f"{ui_url}/runs/flow-run/{run_id}",
        "_deleted": False,
    }


def _hash(val: Any) -> str:
    return hashlib.sha256(json.dumps(val, sort_keys=True, default=str).encode()).hexdigest()[:16]


# ---------------------------------------------------------------------------
# Extraction Walker
# ---------------------------------------------------------------------------
class _CutShortError(Exception):
    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


_ENDPOINTS = {
    "flows": ("/flows/filter", "/flows"),
    "deployments": ("/deployments/filter", "/deployments"),
    "flow_runs": ("/flow_runs/filter", "/flow_runs"),
}


class _Walker:
    """Stateful walker traversing Prefect flows, deployments, and flow runs."""

    def __init__(
        self,
        client: Any,
        ui_url: str,
        state: dict[str, Any],
        stats: dict[str, int],
        *,
        include: tuple[str, ...],
        flow_tags: list[str] | None,
        deployment_tags: list[str] | None,
        flow_run_states: list[str] | None,
        max_requests: int,
        check_active: Callable[[], None] | None,
    ):
        self.client = client
        self.ui_url = ui_url
        self.state = state
        self.stats = stats
        for key in ("scanned", "skipped", "deleted", "failed"):
            stats.setdefault(key, 0)

        self.include = include
        self.flow_tags = sorted(flow_tags or [])
        self.deployment_tags = sorted(deployment_tags or [])
        self.flow_run_states = sorted(flow_run_states or [])
        self.max_requests = max_requests
        self.check_active = check_active

        self.requests = 0
        self.emitted = 0
        self.fingerprints: dict[str, str] = state.setdefault("fingerprints", {})
        self.seen: list[str] = state.setdefault("seen", [])
        self.lookups = PrefectLookups()

    def _call(self, method: str, path: str, **kwargs: Any) -> Any:
        if self.check_active is not None:
            self.check_active()
        self._check_budget()
        self.requests += 1
        try:
            return self.client.request(method, path, **kwargs)
        except PrefectRateLimitedError:
            raise _CutShortError("rate_limit") from None
        except PrefectAuthError:
            if self.emitted:
                raise _CutShortError("auth") from None
            raise

    def _check_budget(self) -> None:
        if self.requests >= self.max_requests:
            raise _CutShortError("budget")

    def _build_filter_body(self, kind: str, offset: int) -> dict[str, Any]:
        body: dict[str, Any] = {
            "limit": PAGE_SIZE,
            "offset": offset,
        }
        if kind == "flows":
            body["sort"] = "CREATED_DESC"
            if self.flow_tags:
                body["flows"] = {"tags": {"all_": self.flow_tags}}
        elif kind == "deployments":
            body["sort"] = "CREATED_DESC"
            if self.deployment_tags:
                body["deployments"] = {"tags": {"all_": self.deployment_tags}}
        elif kind == "flow_runs":
            body["sort"] = "EXPECTED_START_TIME_DESC"
            if self.flow_run_states:
                body["flow_runs"] = {"state": {"type": {"any_": self.flow_run_states}}}
        return body

    def _walk_kind(self, kind: str) -> Iterator[dict[str, Any]]:
        filter_path, _ = _ENDPOINTS[kind]
        walks = self.state.setdefault("walks", {})
        query_sig = _hash(
            {
                "flow_tags": self.flow_tags,
                "dep_tags": self.deployment_tags,
                "states": self.flow_run_states,
            }
        )
        if walks.get(kind, {}).get("query") != query_sig:
            walks[kind] = {"offset": 0, "query": query_sig}
            prefix = f"{kind[:-1] if kind != 'flow_runs' else 'flow_run'}:"
            self.seen[:] = [r_id for r_id in self.seen if not r_id.startswith(prefix)]

        progress = walks[kind]
        if progress.get("done"):
            return

        while True:
            body = self._build_filter_body(kind, progress["offset"])
            records = self._call("POST", filter_path, body=body) or []
            if not isinstance(records, list):
                raise PrefectAPIError(f"Prefect {filter_path} did not return a list")

            yield from records

            if len(records) < PAGE_SIZE:
                break
            progress["offset"] += PAGE_SIZE

        progress["done"] = True

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

    def _flow_rows(self) -> Iterator[dict[str, Any]]:
        for flow in self._walk_kind("flows"):
            self.lookups.flow_names[str(flow["id"])] = _text(flow.get("name"))
            row = render_flow(flow, self.ui_url)
            fp = _hash(row)
            if self._changed(row["id"], fp):
                yield self._emit(row, fp)

    def _deployment_rows(self) -> Iterator[dict[str, Any]]:
        for dep in self._walk_kind("deployments"):
            self.lookups.deployment_names[str(dep["id"])] = _text(dep.get("name"))
            row = render_deployment(dep, self.lookups, self.ui_url)
            fp = _hash(row)
            if self._changed(row["id"], fp):
                yield self._emit(row, fp)

    def _flow_run_rows(self) -> Iterator[dict[str, Any]]:
        for run in self._walk_kind("flow_runs"):
            row = render_flow_run(run, self.lookups, self.ui_url)
            fp = _hash(row)
            if self._changed(row["id"], fp):
                yield self._emit(row, fp)

    def _close_cycle(self) -> None:
        missing = set(self.fingerprints) - set(self.seen)
        self.state["pending_deletes"] = sorted(missing)
        self.state["walks"] = {}
        self.seen.clear()

    def _forget(self) -> Iterator[dict[str, Any]]:
        pending = self.state.setdefault("pending_deletes", [])
        while pending:
            row_id = pending[0]
            try:
                gone = self._is_gone(row_id)
            except (PrefectAPIError, PrefectAccessError) as exc:
                self.stats["failed"] += 1
                logger.warning(
                    "Prefect deletion check failed for one record, retried next run: %s",
                    exc,
                )
                gone = False

            if gone:
                self.fingerprints.pop(row_id, None)
                self.stats["deleted"] += 1
                yield {"id": row_id, "_deleted": True}
            pending.pop(0)

    def _is_gone(self, row_id: str) -> bool:
        prefix, record_id = row_id.split(":", 1)
        kind = f"{prefix}s" if prefix in ("flow", "deployment") else "flow_runs"
        if kind not in self.include:
            return True

        _, single_path = _ENDPOINTS[kind]
        try:
            record = self._call("GET", f"{single_path}/{record_id}")
        except PrefectNotFoundError:
            return True

        if not isinstance(record, dict):
            raise PrefectAPIError(f"Prefect sent unexpected answer for {single_path}/{record_id}")

        # If still in Prefect, check if it was filtered out by active filter criteria
        if kind == "flows" and self.flow_tags:
            rec_tags = set(record.get("tags") or [])
            if not set(self.flow_tags).issubset(rec_tags):
                return True
        elif kind == "deployments" and self.deployment_tags:
            rec_tags = set(record.get("tags") or [])
            if not set(self.deployment_tags).issubset(rec_tags):
                return True
        elif kind == "flow_runs" and self.flow_run_states:
            state_type = record.get("state_type")
            if state_type not in self.flow_run_states:
                return True

        return False

    def rows(self) -> Iterator[dict[str, Any]]:
        yield from self._forget()
        if "flows" in self.include:
            yield from self._flow_rows()
        if "deployments" in self.include:
            yield from self._deployment_rows()
        if "flow_runs" in self.include:
            yield from self._flow_run_rows()
        self._close_cycle()
        yield from self._forget()


def _iter_rows(
    client: Any,
    ui_url: str,
    state: dict[str, Any],
    stats: dict[str, int],
    *,
    include: tuple[str, ...] = KINDS,
    flow_tags: list[str] | None = None,
    deployment_tags: list[str] | None = None,
    flow_run_states: list[str] | None = None,
    max_requests: int = DEFAULT_MAX_REQUESTS,
    check_active: Callable[[], None] | None = None,
) -> Iterator[dict[str, Any]]:
    walker = _Walker(
        client,
        ui_url,
        state,
        stats,
        include=include,
        flow_tags=flow_tags,
        deployment_tags=deployment_tags,
        flow_run_states=flow_run_states,
        max_requests=max_requests,
        check_active=check_active,
    )
    try:
        yield from walker.rows()
    except _CutShortError as cut:
        stats["failed"] = max(1, stats.get("failed", 0))
        stats[f"failed_{cut.reason}"] = 1
        logger.warning(
            "Prefect sync stopped early (%s); it resumes on the next run.",
            cut.reason,
        )


# ---------------------------------------------------------------------------
# Public factory
# ---------------------------------------------------------------------------
def prefect_source(
    *,
    resource_name: str,
    base_url: str = DEFAULT_BASE_URL,
    ui_url: str | None = None,
    api_key: str | None = None,
    service: Any = None,
    check_active: Callable[[], None] | None = None,
    include: tuple[str, ...] = KINDS,
    flow_tags: list[str] | None = None,
    deployment_tags: list[str] | None = None,
    flow_run_states: list[str] | None = None,
    max_requests: int = DEFAULT_MAX_REQUESTS,
):
    """Return a ``dlt`` resource yielding Prefect orchestrator metadata for ``cognee.remember``.

    Args:
        resource_name: Stable name of this connection, one per Prefect workspace.
            Scopes sync state and tombstone cleanup across runs.
        base_url: Base REST API URL of the Prefect server or Cloud workspace.
            Defaults to http://localhost:4200/api.
        ui_url: Optional web UI root URL for deep links. If omitted, inferred from base_url.
        api_key: Optional Prefect Cloud or authenticated server API token.
        service: Injected client instance, primarily for offline tests.
        check_active: Optional host checkpoint called before every HTTP request.
        include: Subset of ("flows", "deployments", "flow_runs") to sync.
        flow_tags: Only sync flows containing all of these tags.
        deployment_tags: Only sync deployments containing all of these tags.
        flow_run_states: Only sync flow runs in these state types (e.g. ["COMPLETED", "FAILED"]).
        max_requests: Maximum requests permitted per run before clean interruption.

    Returns:
        A ``dlt`` resource configured with primary_key="id", write_disposition="merge",
        and a "_deleted" hard-delete marker.
    """
    import dlt

    if getattr(dlt_utils, "DOCUMENT_SYNC_VERSION", 0) < 1:
        raise RuntimeError("Prefect sync requires cognee>=1.6.3 (table-scoped document cleanup).")
    if not isinstance(resource_name, str) or not resource_name.strip():
        raise ValueError("prefect_source needs a valid resource_name")

    include = tuple(include)
    if not include or set(include) - set(KINDS):
        raise ValueError(f"include must be a non-empty subset of {KINDS}")

    client = service if service is not None else PrefectClient(base_url=base_url, api_key=api_key)
    raw_ui = (ui_url or "").rstrip("/")
    resolved_ui_url = raw_ui or _infer_ui_url(getattr(client, "base_url", base_url))
    stats: dict[str, int] = {}

    @dlt.resource(
        name=resource_name,
        primary_key="id",
        write_disposition="merge",
        columns={"_deleted": {"data_type": "bool", "hard_delete": True}},
    )
    def prefect_workspace():
        stats.clear()
        stats.update(scanned=0, skipped=0, deleted=0, failed=0)
        if hasattr(client, "rate_limit"):
            client.rate_limit = {}

        rows = _iter_rows(
            client,
            resolved_ui_url,
            dlt.current.resource_state(),
            stats,
            include=include,
            flow_tags=flow_tags,
            deployment_tags=deployment_tags,
            flow_run_states=flow_run_states,
            max_requests=max_requests,
            check_active=check_active,
        )
        yield from dlt_utils.guarded_rows(rows, check_active)

    res = prefect_workspace()
    setattr(res, DOCUMENT_SOURCE_ATTR, "prefect")
    setattr(res, dlt_utils.PIPELINE_SCOPE_ATTR, resource_name)
    res.cognee_sync_stats = stats
    return res
