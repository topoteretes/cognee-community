"""Checkly synthetic API and browser monitoring connector for cognee.

A dlt resource that syncs your Checkly synthetic checks, check groups, alert channels,
and maintenance windows into cognee memory, enabling natural language questions over
service reliability, health checks, and degradation alerts:

    await cognee.remember(
        checkly_source(
            resource_name="checkly_prod",
            api_key="<checkly api key>",
            account_id="<optional account id>",
        ),
        dataset_name="checkly_monitoring",
        primary_key="id",
        write_disposition="merge",
    )
"""

import hashlib
import json
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlparse

import requests
from cognee.shared.logging_utils import get_logger
from cognee.tasks.ingestion import dlt_utils
from cognee.tasks.ingestion.dlt_utils import DOCUMENT_SOURCE_ATTR

logger = get_logger("checkly_connector")

DEFAULT_BASE_URL = "https://api.checklyhq.com/v1"
DEFAULT_DASHBOARD_URL = "https://app.checklyhq.com"
KINDS = ("checks", "check_groups", "alert_channels", "maintenance_windows")
PAGE_SIZE = 100
DEFAULT_MAX_REQUESTS = 500
_TIMEOUT = (10, 30)
_RETRIES = 3


class ChecklySourceError(RuntimeError):
    """Base class for errors raised by this source."""


class ChecklyAPIError(ChecklySourceError):
    """Checkly API answered with an error or could not be reached."""


class ChecklyAuthError(ChecklySourceError):
    """Checkly rejected authentication (HTTP 401)."""


class ChecklyAccessError(ChecklySourceError):
    """Checkly refused access to a resource or account (HTTP 403)."""


class ChecklyRateLimitedError(ChecklySourceError):
    """Checkly rate limit exceeded (HTTP 429)."""


class ChecklyNotFoundError(ChecklyAPIError):
    """The requested Checkly resource was not found (HTTP 404)."""


def _int_header(headers: Any, name: str) -> int | None:
    try:
        return int(headers.get(name))
    except (TypeError, ValueError):
        return None


def _clean_endpoint(path: str) -> str:
    parts = path.strip("/").split("/")
    if len(parts) >= 2 and parts[-1].isdigit():
        return f"/{parts[0]}/{{id}}"
    return path


class ChecklyClient:
    """Minimal synchronous REST client for the Checkly API."""

    def __init__(
        self,
        api_key: str,
        account_id: str | None = None,
        base_url: str = DEFAULT_BASE_URL,
        *,
        sleep: Callable[[float], None] = time.sleep,
    ):
        key = (api_key or "").strip()
        if not key or not key.isascii() or not key.isprintable() or " " in key:
            raise ValueError("The Checkly API key is empty or has an invalid format")

        base = (base_url or "").strip().rstrip("/")
        if not base:
            raise ValueError("The Checkly base_url cannot be empty")
        parsed = urlparse(base)
        if not parsed.scheme or not parsed.netloc:
            raise ValueError(f"Invalid Checkly base_url: {base_url!r}")

        self.base_url = base
        self._api_key = key
        self.account_id = (account_id or "").strip() or None
        self._sleep = sleep
        self.rate_limit: dict[str, int | None] = {}

    def __repr__(self) -> str:
        acc_repr = f", account_id={self.account_id!r}" if self.account_id else ""
        return f"ChecklyClient(base_url={self.base_url!r}, api_key=<redacted>{acc_repr})"

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
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
            "Cache-Control": "no-cache",
        }
        if self.account_id:
            headers["X-Checkly-Account"] = self.account_id

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
                    raise ChecklyAPIError(f"Checkly request failed: {type(exc).__name__}") from None
                self._sleep(2**attempt)
                continue

            if response.status_code >= 500 and not last:
                self._sleep(2**attempt)
                continue

            return self._parse(response, path)

    def _parse(self, response: Any, path: str) -> Any:
        status = response.status_code
        if status == 401:
            raise ChecklyAuthError("Checkly rejected authentication (HTTP 401)")
        if status == 403:
            raise ChecklyAccessError(f"Checkly denied access to {_clean_endpoint(path)} (HTTP 403)")
        if status == 404:
            raise ChecklyNotFoundError(f"Checkly record not found (HTTP 404): {path}")
        if status == 429:
            raise ChecklyRateLimitedError("Checkly rate limit exceeded (HTTP 429)")

        try:
            payload = response.json()
        except ValueError:
            payload = None

        if status not in (200, 201):
            raise ChecklyAPIError(f"Checkly request failed: HTTP {status}")

        if not isinstance(payload, (list, dict)):
            raise ChecklyAPIError(
                f"Checkly sent an unexpected response format for {_clean_endpoint(path)}"
            )

        headers = response.headers
        self.rate_limit = {
            "remaining": _int_header(headers, "x-ratelimit-remaining"),
            "reset": _int_header(headers, "x-ratelimit-reset"),
        }
        return payload


# ---------------------------------------------------------------------------
# Rendering: deterministic representations with strictly redacted credentials
# ---------------------------------------------------------------------------
@dataclass
class ChecklyLookups:
    """Lookup cache mapping group IDs to names and check IDs to latest statuses."""

    group_names: dict[str, str] = field(default_factory=dict)
    check_statuses: dict[str, str] = field(default_factory=dict)


def _text(val: Any) -> str:
    if isinstance(val, list):
        return ", ".join(sorted(_text(x) for x in val if _text(x)))
    return "" if val is None else str(val).strip()


def _lines(pairs: list[tuple[str, str]]) -> list[str]:
    return [f"{label}: {value}" for label, value in pairs if value]


def _header_keys(headers: Any) -> str:
    """Redact sensitive header values (tokens, cookies, auth) completely."""
    if not isinstance(headers, list) or not headers:
        return "None"
    keys = [h.get("key") for h in headers if isinstance(h, dict) and h.get("key")]
    return ", ".join(sorted(str(k) for k in keys)) if keys else "None"


def _env_keys(env_vars: Any) -> str:
    """Extract environment variable keys only, omitting secret values."""
    if not isinstance(env_vars, list) or not env_vars:
        return "None"
    keys = [e.get("key") for e in env_vars if isinstance(e, dict) and e.get("key")]
    return ", ".join(sorted(str(k) for k in keys)) if keys else "None"


def render_check(
    check: dict[str, Any], lookups: ChecklyLookups, dashboard_url: str
) -> dict[str, Any]:
    check_id = str(check["id"])
    name = _text(check.get("name")) or "Unnamed Check"
    check_type = _text(check.get("checkType")) or "API"
    activated = "active" if check.get("activated", True) else "paused"

    group_id = str(check.get("groupId") or "")
    group_name = lookups.group_names.get(group_id, "None (Standalone)")

    status = lookups.check_statuses.get(check_id, "UNKNOWN")

    req = check.get("request") or {}
    method = _text(req.get("method")) or "GET"
    url = _text(req.get("url")) or "Script-based / E2E"
    headers_summary = _header_keys(req.get("headers"))

    fields = [
        ("Type", f"Check ({check_type})"),
        ("Name", name),
        ("Current Status", status),
        ("Group", group_name),
        ("State", activated),
        ("Method", method),
        ("Target URL", url),
        ("Frequency", f"{check.get('frequency', 5)}m"),
        ("Locations", _text(check.get("locations"))),
        ("Tags", _text(check.get("tags"))),
        ("Headers (keys only)", headers_summary),
        ("Created", _text(check.get("created_at"))),
        ("Updated", _text(check.get("updated_at"))),
    ]
    return {
        "id": f"check:{check_id}",
        "title": f"Check: {name} [{check_type} - {status}]",
        "content": "\n".join(_lines(fields)),
        "url": f"{dashboard_url}/checks/{check_id}",
        "_deleted": False,
    }


def render_check_group(group: dict[str, Any], dashboard_url: str) -> dict[str, Any]:
    group_id = str(group["id"])
    name = _text(group.get("name")) or "Unnamed Group"
    activated = "active" if group.get("activated", True) else "paused"
    concurrency = _text(group.get("concurrency")) or "Default"
    env_summary = _env_keys(group.get("environmentVariables"))

    fields = [
        ("Type", "Check Group"),
        ("Name", name),
        ("State", activated),
        ("Concurrency", concurrency),
        ("Locations", _text(group.get("locations"))),
        ("Tags", _text(group.get("tags"))),
        ("Environment Variables (keys only)", env_summary),
        ("Created", _text(group.get("created_at"))),
        ("Updated", _text(group.get("updated_at"))),
    ]
    return {
        "id": f"check_group:{group_id}",
        "title": f"Check Group: {name}",
        "content": "\n".join(_lines(fields)),
        "url": f"{dashboard_url}/checks?groupId={group_id}",
        "_deleted": False,
    }


def render_alert_channel(channel: dict[str, Any], dashboard_url: str) -> dict[str, Any]:
    ch_id = str(channel["id"])
    name = _text(channel.get("name")) or "Unnamed Channel"
    ch_type = _text(channel.get("type")) or "WEBHOOK"

    recovery = "Yes" if channel.get("sendRecovery") else "No"
    failure = "Yes" if channel.get("sendFailure") else "No"
    degraded = "Yes" if channel.get("sendDegraded") else "No"

    fields = [
        ("Type", f"Alert Channel ({ch_type})"),
        ("Name", name),
        ("Channel Type", ch_type),
        ("Send on Failure", failure),
        ("Send on Recovery", recovery),
        ("Send on Degraded", degraded),
        ("Config", "[Configured & Protected]"),
        ("Created", _text(channel.get("created_at"))),
        ("Updated", _text(channel.get("updated_at"))),
    ]
    return {
        "id": f"alert_channel:{ch_id}",
        "title": f"Alert Channel: {name} [{ch_type}]",
        "content": "\n".join(_lines(fields)),
        "url": f"{dashboard_url}/alerts",
        "_deleted": False,
    }


def render_maintenance_window(window: dict[str, Any], dashboard_url: str) -> dict[str, Any]:
    mw_id = str(window["id"])
    name = _text(window.get("name")) or "Unnamed Window"
    repeat = _text(window.get("repeatUnit")) or "NONE"

    fields = [
        ("Type", "Maintenance Window"),
        ("Name", name),
        ("Starts At", _text(window.get("startsAt"))),
        ("Ends At", _text(window.get("endsAt"))),
        ("Repeat Frequency", repeat),
        ("Tags", _text(window.get("tags"))),
    ]
    return {
        "id": f"maintenance_window:{mw_id}",
        "title": f"Maintenance Window: {name}",
        "content": "\n".join(_lines(fields)),
        "url": f"{dashboard_url}/maintenance-windows",
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
    "checks": "/checks",
    "check_groups": "/check-groups",
    "alert_channels": "/alert-channels",
    "maintenance_windows": "/maintenance-windows",
}


class _Walker:
    """Stateful walker traversing Checkly monitoring resources."""

    def __init__(
        self,
        client: Any,
        dashboard_url: str,
        state: dict[str, Any],
        stats: dict[str, int],
        *,
        include: tuple[str, ...],
        check_tags: list[str] | None,
        group_tags: list[str] | None,
        check_types: list[str] | None,
        only_active: bool,
        max_requests: int,
        check_active: Callable[[], None] | None,
    ):
        self.client = client
        self.dashboard_url = dashboard_url
        self.state = state
        self.stats = stats
        for key in ("scanned", "skipped", "deleted", "failed"):
            stats.setdefault(key, 0)

        self.include = include
        self.check_tags = sorted(check_tags or [])
        self.group_tags = sorted(group_tags or [])
        self.check_types = sorted(check_types or [])
        self.only_active = only_active
        self.max_requests = max_requests
        self.check_active = check_active

        self.requests = 0
        self.emitted = 0
        self.fingerprints: dict[str, str] = state.setdefault("fingerprints", {})
        self.seen: list[str] = state.setdefault("seen", [])
        self.lookups = ChecklyLookups()

    def _call(self, method: str, path: str, **kwargs: Any) -> Any:
        if self.check_active is not None:
            self.check_active()
        self._check_budget()
        self.requests += 1
        try:
            return self.client.request(method, path, **kwargs)
        except ChecklyRateLimitedError:
            raise _CutShortError("rate_limit") from None
        except ChecklyAuthError:
            if self.emitted:
                raise _CutShortError("auth") from None
            raise

    def _check_budget(self) -> None:
        if self.requests >= self.max_requests:
            raise _CutShortError("budget")

    def _load_lookups(self) -> None:
        try:
            statuses = self._call("GET", "/check-statuses") or []
            if isinstance(statuses, list):
                for s in statuses:
                    c_id = str(s.get("checkId"))
                    if s.get("hasFailures"):
                        st = "FAILING"
                    elif s.get("isDegraded"):
                        st = "DEGRADED"
                    else:
                        st = "HEALTHY"
                    self.lookups.check_statuses[c_id] = st
        except (ChecklyAPIError, ChecklyAccessError):
            logger.warning("Checkly check-statuses fetch failed, continuing without status cache")

    def _walk_kind(self, kind: str) -> Iterator[dict[str, Any]]:
        path = _ENDPOINTS[kind]
        walks = self.state.setdefault("walks", {})
        query_sig = _hash(
            {
                "check_tags": self.check_tags,
                "group_tags": self.group_tags,
                "check_types": self.check_types,
                "only_active": self.only_active,
            }
        )
        if walks.get(kind, {}).get("query") != query_sig:
            walks[kind] = {"page": 1, "query": query_sig}
            prefix = f"{kind[:-1]}:"
            self.seen[:] = [r_id for r_id in self.seen if not r_id.startswith(prefix)]

        progress = walks[kind]
        if progress.get("done"):
            return

        while True:
            params = {"page": progress["page"], "limit": PAGE_SIZE}
            records = self._call("GET", path, params=params) or []
            if not isinstance(records, list):
                raise ChecklyAPIError(f"Checkly {path} did not return a list")

            yield from records

            if len(records) < PAGE_SIZE:
                break
            progress["page"] += 1

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

    def _check_group_rows(self) -> Iterator[dict[str, Any]]:
        for group in self._walk_kind("check_groups"):
            g_id = str(group["id"])
            self.lookups.group_names[g_id] = _text(group.get("name"))
            if self.group_tags:
                g_tags = set(group.get("tags") or [])
                if not set(self.group_tags).issubset(g_tags):
                    continue
            if self.only_active and not group.get("activated", True):
                continue

            row = render_check_group(group, self.dashboard_url)
            fp = _hash(row)
            if self._changed(row["id"], fp):
                yield self._emit(row, fp)

    def _check_rows(self) -> Iterator[dict[str, Any]]:
        for check in self._walk_kind("checks"):
            if self.check_tags:
                c_tags = set(check.get("tags") or [])
                if not set(self.check_tags).issubset(c_tags):
                    continue
            if self.check_types and check.get("checkType") not in self.check_types:
                continue
            if self.only_active and not check.get("activated", True):
                continue

            row = render_check(check, self.lookups, self.dashboard_url)
            fp = _hash(row)
            if self._changed(row["id"], fp):
                yield self._emit(row, fp)

    def _alert_channel_rows(self) -> Iterator[dict[str, Any]]:
        for ch in self._walk_kind("alert_channels"):
            row = render_alert_channel(ch, self.dashboard_url)
            fp = _hash(row)
            if self._changed(row["id"], fp):
                yield self._emit(row, fp)

    def _maintenance_window_rows(self) -> Iterator[dict[str, Any]]:
        for mw in self._walk_kind("maintenance_windows"):
            row = render_maintenance_window(mw, self.dashboard_url)
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
            except (ChecklyAPIError, ChecklyAccessError) as exc:
                self.stats["failed"] += 1
                logger.warning(
                    "Checkly deletion check failed for one record, retried next run: %s",
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
        kind_map = {
            "check": "checks",
            "check_group": "check_groups",
            "alert_channel": "alert_channels",
            "maintenance_window": "maintenance_windows",
        }
        kind = kind_map.get(prefix)
        if not kind or kind not in self.include:
            return True

        single_path = f"{_ENDPOINTS[kind]}/{record_id}"
        try:
            record = self._call("GET", single_path)
        except ChecklyNotFoundError:
            return True

        if not isinstance(record, dict):
            raise ChecklyAPIError(f"Checkly sent unexpected answer for {single_path}")

        # Check if record fell out of active filter scope
        if kind == "checks":
            if self.check_tags and not set(self.check_tags).issubset(set(record.get("tags") or [])):
                return True
            if self.check_types and record.get("checkType") not in self.check_types:
                return True
            if self.only_active and not record.get("activated", True):
                return True
        elif kind == "check_groups":
            if self.group_tags and not set(self.group_tags).issubset(set(record.get("tags") or [])):
                return True
            if self.only_active and not record.get("activated", True):
                return True

        return False

    def rows(self) -> Iterator[dict[str, Any]]:
        self._load_lookups()
        yield from self._forget()
        if "check_groups" in self.include:
            yield from self._check_group_rows()
        if "checks" in self.include:
            yield from self._check_rows()
        if "alert_channels" in self.include:
            yield from self._alert_channel_rows()
        if "maintenance_windows" in self.include:
            yield from self._maintenance_window_rows()
        self._close_cycle()
        yield from self._forget()


def _iter_rows(
    client: Any,
    dashboard_url: str,
    state: dict[str, Any],
    stats: dict[str, int],
    *,
    include: tuple[str, ...] = KINDS,
    check_tags: list[str] | None = None,
    group_tags: list[str] | None = None,
    check_types: list[str] | None = None,
    only_active: bool = False,
    max_requests: int = DEFAULT_MAX_REQUESTS,
    check_active: Callable[[], None] | None = None,
) -> Iterator[dict[str, Any]]:
    walker = _Walker(
        client,
        dashboard_url,
        state,
        stats,
        include=include,
        check_tags=check_tags,
        group_tags=group_tags,
        check_types=check_types,
        only_active=only_active,
        max_requests=max_requests,
        check_active=check_active,
    )
    try:
        yield from walker.rows()
    except _CutShortError as cut:
        stats["failed"] = max(1, stats.get("failed", 0))
        stats[f"failed_{cut.reason}"] = 1
        logger.warning(
            "Checkly sync stopped early (%s); it resumes on the next run.",
            cut.reason,
        )


# ---------------------------------------------------------------------------
# Public factory
# ---------------------------------------------------------------------------
def checkly_source(
    *,
    resource_name: str,
    api_key: str | None = None,
    account_id: str | None = None,
    base_url: str = DEFAULT_BASE_URL,
    dashboard_url: str = DEFAULT_DASHBOARD_URL,
    service: Any = None,
    check_active: Callable[[], None] | None = None,
    include: tuple[str, ...] = KINDS,
    check_tags: list[str] | None = None,
    group_tags: list[str] | None = None,
    check_types: list[str] | None = None,
    only_active: bool = False,
    max_requests: int = DEFAULT_MAX_REQUESTS,
):
    """Return a ``dlt`` resource yielding Checkly synthetic monitoring data for ``cognee.remember``.

    Args:
        resource_name: Stable connection identifier scoping sync state and tombstones.
        api_key: Checkly API key (Bearer token).
        account_id: Optional Checkly account ID (sent via X-Checkly-Account).
        base_url: Base REST API URL (defaults to https://api.checklyhq.com/v1).
        dashboard_url: Checkly dashboard URL for deep links (defaults to https://app.checklyhq.com).
        service: Injected client instance for offline test fixtures.
        check_active: Checkpoint callable invoked before each HTTP request.
        include: Subset of ("checks", "check_groups", "alert_channels", "maintenance_windows").
        check_tags: Only sync checks matching all of these tags.
        group_tags: Only sync groups matching all of these tags.
        check_types: Only sync checks matching these types (e.g. ["API", "BROWSER"]).
        only_active: If True, skips paused checks and groups.
        max_requests: Maximum request budget per run.

    Returns:
        A ``dlt`` resource configured with write_disposition="merge", primary_key="id",
        and a "_deleted" hard-delete marker.
    """
    import dlt

    if getattr(dlt_utils, "DOCUMENT_SYNC_VERSION", 0) < 1:
        raise RuntimeError("Checkly sync requires cognee>=1.6.3 (table-scoped document cleanup).")
    if not isinstance(resource_name, str) or not resource_name.strip():
        raise ValueError("checkly_source needs a valid resource_name")

    include = tuple(include)
    if not include or set(include) - set(KINDS):
        raise ValueError(f"include must be a non-empty subset of {KINDS}")

    client = (
        service
        if service is not None
        else ChecklyClient(api_key=api_key or "", account_id=account_id, base_url=base_url)
    )
    resolved_dash = (dashboard_url or DEFAULT_DASHBOARD_URL).rstrip("/")
    stats: dict[str, int] = {}

    @dlt.resource(
        name=resource_name,
        primary_key="id",
        write_disposition="merge",
        columns={"_deleted": {"data_type": "bool", "hard_delete": True}},
    )
    def checkly_workspace():
        stats.clear()
        stats.update(scanned=0, skipped=0, deleted=0, failed=0)
        if hasattr(client, "rate_limit"):
            client.rate_limit = {}

        rows = _iter_rows(
            client,
            resolved_dash,
            dlt.current.resource_state(),
            stats,
            include=include,
            check_tags=check_tags,
            group_tags=group_tags,
            check_types=check_types,
            only_active=only_active,
            max_requests=max_requests,
            check_active=check_active,
        )
        yield from dlt_utils.guarded_rows(rows, check_active)

    res = checkly_workspace()
    setattr(res, DOCUMENT_SOURCE_ATTR, "checkly")
    setattr(res, dlt_utils.PIPELINE_SCOPE_ATTR, resource_name)
    res.cognee_sync_stats = stats
    return res
