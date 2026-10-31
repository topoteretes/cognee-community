"""UptimeRobot website uptime and monitor status connector for cognee.

A dlt resource that syncs your UptimeRobot monitors, public status pages,
alert contacts, and maintenance windows into cognee memory, enabling natural
language questions over live service availability, incident downtimes, and uptime SLAs:

    await cognee.remember(
        uptimerobot_source(
            resource_name="uptimerobot_prod",
            api_key="<uptimerobot api key>",
        ),
        dataset_name="uptime_monitoring",
        primary_key="id",
        write_disposition="merge",
    )
"""

import hashlib
import json
import time
from collections.abc import Callable, Iterator
from typing import Any
from urllib.parse import urlparse

import requests
from cognee.shared.logging_utils import get_logger
from cognee.tasks.ingestion import dlt_utils
from cognee.tasks.ingestion.dlt_utils import DOCUMENT_SOURCE_ATTR

logger = get_logger("uptimerobot_connector")

DEFAULT_BASE_URL = "https://api.uptimerobot.com/v2"
DEFAULT_DASHBOARD_URL = "https://uptimerobot.com/dashboard"
KINDS = ("monitors", "public_status_pages", "alert_contacts", "maintenance_windows")
PAGE_SIZE = 50
DEFAULT_MAX_REQUESTS = 500
_TIMEOUT = (10, 30)
_RETRIES = 3

MONITOR_STATUS_MAP = {
    0: "PAUSED",
    1: "NOT_CHECKED_YET",
    2: "UP",
    8: "SEEMS_DOWN",
    9: "DOWN",
}

MONITOR_TYPE_MAP = {
    1: "HTTP(s)",
    2: "Keyword",
    3: "Ping",
    4: "Port",
    5: "Heartbeat",
}

ALERT_TYPE_MAP = {
    1: "SMS",
    2: "Email",
    3: "Twitter",
    4: "Boxcar",
    5: "Pushbullet",
    6: "Zapier",
    11: "Slack",
    14: "PagerDuty",
    19: "Webhook",
}


class UptimeRobotSourceError(RuntimeError):
    """Base class for errors raised by this source."""


class UptimeRobotAPIError(UptimeRobotSourceError):
    """UptimeRobot API answered with an error or could not be reached."""


class UptimeRobotAuthError(UptimeRobotSourceError):
    """UptimeRobot rejected authentication (invalid or expired API key)."""


class UptimeRobotAccessError(UptimeRobotSourceError):
    """UptimeRobot refused access to the requested endpoint or resource."""


class UptimeRobotRateLimitedError(UptimeRobotSourceError):
    """UptimeRobot rate limit exceeded."""


class UptimeRobotNotFoundError(UptimeRobotAPIError):
    """The requested UptimeRobot object was not found."""


def _int_header(headers: Any, name: str) -> int | None:
    try:
        return int(headers.get(name))
    except (TypeError, ValueError):
        return None


class UptimeRobotClient:
    """Minimal synchronous REST client for the UptimeRobot API v2."""

    def __init__(
        self,
        api_key: str,
        base_url: str = DEFAULT_BASE_URL,
        *,
        sleep: Callable[[float], None] = time.sleep,
    ):
        key = (api_key or "").strip()
        if not key or not key.isascii() or not key.isprintable() or " " in key:
            raise ValueError("The UptimeRobot API key is empty or has an invalid format")

        base = (base_url or "").strip().rstrip("/")
        if not base:
            raise ValueError("The UptimeRobot base_url cannot be empty")
        parsed = urlparse(base)
        if not parsed.scheme or not parsed.netloc:
            raise ValueError(f"Invalid UptimeRobot base_url: {base_url!r}")

        self.base_url = base
        self._api_key = key
        self._sleep = sleep
        self.rate_limit: dict[str, int | None] = {}

    def __repr__(self) -> str:
        return f"UptimeRobotClient(base_url={self.base_url!r}, api_key=<redacted>)"

    def request(
        self,
        endpoint: str,
        *,
        body: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        url = f"{self.base_url}/{endpoint.lstrip('/')}"
        payload = {"api_key": self._api_key, "format": "json", **(body or {})}
        headers = {
            "Content-Type": "application/json",
            "Cache-Control": "no-cache",
        }

        for attempt in range(_RETRIES):
            last = attempt == _RETRIES - 1
            try:
                response = requests.post(
                    url,
                    json=payload,
                    headers=headers,
                    timeout=_TIMEOUT,
                )
            except requests.RequestException as exc:
                if last:
                    raise UptimeRobotAPIError(
                        f"UptimeRobot request failed: {type(exc).__name__}"
                    ) from None
                self._sleep(2**attempt)
                continue

            if response.status_code >= 500 and not last:
                self._sleep(2**attempt)
                continue

            return self._parse(response, endpoint)

    def _parse(self, response: Any, endpoint: str) -> dict[str, Any]:
        status = response.status_code
        if status == 401:
            raise UptimeRobotAuthError("UptimeRobot rejected authentication (HTTP 401)")
        if status == 403:
            raise UptimeRobotAccessError(f"UptimeRobot denied access to {endpoint} (HTTP 403)")
        if status == 404:
            raise UptimeRobotNotFoundError(f"UptimeRobot endpoint not found (HTTP 404): {endpoint}")
        if status == 429:
            raise UptimeRobotRateLimitedError("UptimeRobot rate limit exceeded (HTTP 429)")

        try:
            data = response.json()
        except ValueError:
            data = None

        if status not in (200, 201):
            raise UptimeRobotAPIError(f"UptimeRobot request failed: HTTP {status}")

        if not isinstance(data, dict):
            raise UptimeRobotAPIError(
                f"UptimeRobot sent an unexpected response structure for {endpoint}"
            )

        if data.get("stat") == "fail":
            err = data.get("error") or {}
            err_msg = err.get("message") or str(err)
            err_type = err.get("type", "")
            if "api_key" in err_msg.lower() or "auth" in err_msg.lower():
                raise UptimeRobotAuthError(f"UptimeRobot auth failed: {err_msg}")
            if "rate_limit" in err_type.lower() or "limit" in err_msg.lower():
                raise UptimeRobotRateLimitedError(f"UptimeRobot rate limited: {err_msg}")
            if "not found" in err_msg.lower():
                raise UptimeRobotNotFoundError(f"UptimeRobot record not found: {err_msg}")
            raise UptimeRobotAPIError(f"UptimeRobot error: {err_msg}")

        headers = response.headers
        self.rate_limit = {
            "remaining": _int_header(headers, "x-ratelimit-remaining"),
            "reset": _int_header(headers, "x-ratelimit-reset"),
        }
        return data


# ---------------------------------------------------------------------------
# Rendering: deterministic representations with strictly redacted credentials
# ---------------------------------------------------------------------------
def _text(val: Any) -> str:
    if isinstance(val, list):
        return ", ".join(sorted(_text(x) for x in val if _text(x)))
    return "" if val is None else str(val).strip()


def _lines(pairs: list[tuple[str, str]]) -> list[str]:
    return [f"{label}: {value}" for label, value in pairs if value]


def _format_uptime_ratio(ratio_str: Any) -> str:
    if not ratio_str or not isinstance(ratio_str, str):
        return "N/A"
    parts = ratio_str.split("-")
    labels = ("24h", "7d", "30d")
    return ", ".join(f"{lbl}: {p}%" for lbl, p in zip(labels, parts, strict=False) if p)


def render_monitor(monitor: dict[str, Any], dashboard_url: str) -> dict[str, Any]:
    m_id = str(monitor["id"])
    name = _text(monitor.get("friendly_name")) or f"Monitor {m_id}"
    url = _text(monitor.get("url")) or "N/A"

    m_type_code = monitor.get("type", 1)
    type_name = MONITOR_TYPE_MAP.get(m_type_code, f"Type {m_type_code}")

    st_code = monitor.get("status", 1)
    status_name = MONITOR_STATUS_MAP.get(st_code, f"Status {st_code}")

    ratios = _format_uptime_ratio(monitor.get("custom_uptime_ratio"))
    resp_time = monitor.get("average_response_time")
    resp_time_str = f"{float(resp_time):.1f}ms" if resp_time else "N/A"

    fields = [
        ("Type", f"Monitor ({type_name})"),
        ("Name", name),
        ("Current Status", status_name),
        ("Target URL", url),
        ("Check Interval", f"{monitor.get('interval', 300)}s"),
        ("Uptime Ratios", ratios),
        ("Average Response Time", resp_time_str),
        ("HTTP Auth", "[Redacted & Protected]" if monitor.get("http_username") else "None"),
    ]
    return {
        "id": f"monitor:{m_id}",
        "title": f"Monitor: {name} [{type_name} - {status_name}]",
        "content": "\n".join(_lines(fields)),
        "url": f"{dashboard_url}#monitor={m_id}",
        "_deleted": False,
    }


def render_psp(psp: dict[str, Any], dashboard_url: str) -> dict[str, Any]:
    psp_id = str(psp["id"])
    name = _text(psp.get("friendly_name")) or f"Status Page {psp_id}"
    custom = _text(psp.get("custom_domain"))
    standard = _text(psp.get("standard_url"))
    domain = custom or standard or "N/A"
    monitors_count = len(psp.get("monitors") or [])
    status = "Active" if psp.get("status") == 1 else "Paused"

    if custom:
        psp_url = custom if custom.startswith("http") else f"https://{custom}"
    elif standard:
        psp_url = standard
    else:
        psp_url = f"{dashboard_url}#psp={psp_id}"

    fields = [
        ("Type", "Public Status Page"),
        ("Name", name),
        ("Status", status),
        ("Domain / URL", domain),
        ("Assigned Monitors Count", str(monitors_count)),
    ]
    return {
        "id": f"psp:{psp_id}",
        "title": f"Status Page: {name} [{status}]",
        "content": "\n".join(_lines(fields)),
        "url": psp_url,
        "_deleted": False,
    }


def render_alert_contact(contact: dict[str, Any], dashboard_url: str) -> dict[str, Any]:
    c_id = str(contact["id"])
    name = _text(contact.get("friendly_name")) or f"Alert Contact {c_id}"
    type_code = contact.get("type", 1)
    type_name = ALERT_TYPE_MAP.get(type_code, f"Channel {type_code}")
    status = "Active" if contact.get("status") == 2 else "Pending / Paused"

    fields = [
        ("Type", f"Alert Contact ({type_name})"),
        ("Name", name),
        ("Channel Type", type_name),
        ("Status", status),
        ("Endpoint / Destination", "[Configured & Protected]"),
    ]
    return {
        "id": f"alert_contact:{c_id}",
        "title": f"Alert Contact: {name} [{type_name}]",
        "content": "\n".join(_lines(fields)),
        "url": f"{dashboard_url}#mySettings",
        "_deleted": False,
    }


def render_mwindow(mw: dict[str, Any], dashboard_url: str) -> dict[str, Any]:
    mw_id = str(mw["id"])
    name = _text(mw.get("friendly_name")) or f"Maintenance Window {mw_id}"
    duration = mw.get("duration", 0)

    fields = [
        ("Type", "Maintenance Window"),
        ("Name", name),
        ("Duration", f"{duration} minutes"),
        ("Starts At", _text(mw.get("value"))),
        ("Status", "Active" if mw.get("status") == 1 else "Paused"),
    ]
    return {
        "id": f"mwindow:{mw_id}",
        "title": f"Maintenance Window: {name}",
        "content": "\n".join(_lines(fields)),
        "url": f"{dashboard_url}#mySettings",
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
    "monitors": "getMonitors",
    "public_status_pages": "getPSPs",
    "alert_contacts": "getAlertContacts",
    "maintenance_windows": "getMWindows",
}

_PAYLOAD_KEYS = {
    "monitors": "monitors",
    "public_status_pages": "psps",
    "alert_contacts": "alert_contacts",
    "maintenance_windows": "mwindows",
}


class _Walker:
    """Stateful walker traversing UptimeRobot monitoring resources."""

    def __init__(
        self,
        client: Any,
        dashboard_url: str,
        state: dict[str, Any],
        stats: dict[str, int],
        *,
        include: tuple[str, ...],
        monitor_types: list[str] | None,
        only_down: bool,
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
        self.monitor_types = sorted(monitor_types or [])
        self.only_down = only_down
        self.max_requests = max_requests
        self.check_active = check_active

        self.requests = 0
        self.emitted = 0
        self.fingerprints: dict[str, str] = state.setdefault("fingerprints", {})
        self.seen: list[str] = state.setdefault("seen", [])

    def _call(self, endpoint: str, **kwargs: Any) -> dict[str, Any]:
        if self.check_active is not None:
            self.check_active()
        self._check_budget()
        self.requests += 1
        try:
            return self.client.request(endpoint, **kwargs)
        except UptimeRobotRateLimitedError:
            raise _CutShortError("rate_limit") from None
        except UptimeRobotAuthError:
            if self.emitted:
                raise _CutShortError("auth") from None
            raise

    def _check_budget(self) -> None:
        if self.requests >= self.max_requests:
            raise _CutShortError("budget")

    def _walk_kind(self, kind: str) -> Iterator[dict[str, Any]]:
        endpoint = _ENDPOINTS[kind]
        data_key = _PAYLOAD_KEYS[kind]
        walks = self.state.setdefault("walks", {})
        query_sig = _hash(
            {
                "monitor_types": self.monitor_types,
                "only_down": self.only_down,
            }
        )
        if walks.get(kind, {}).get("query") != query_sig:
            walks[kind] = {"offset": 0, "query": query_sig}
            prefix = f"{kind[:-1]}:"
            self.seen[:] = [r_id for r_id in self.seen if not r_id.startswith(prefix)]

        progress = walks[kind]
        if progress.get("done"):
            return

        while True:
            body: dict[str, Any] = {"offset": progress["offset"], "limit": PAGE_SIZE}
            if kind == "monitors":
                body["response_times"] = 1
                body["custom_uptime_ratios"] = "1-7-30"

            res = self._call(endpoint, body=body) or {}
            records = res.get(data_key) or []
            if not isinstance(records, list):
                raise UptimeRobotAPIError(
                    f"UptimeRobot {endpoint} answered without {data_key} list"
                )

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

    def _monitor_rows(self) -> Iterator[dict[str, Any]]:
        for m in self._walk_kind("monitors"):
            type_name = MONITOR_TYPE_MAP.get(m.get("type", 1), "")
            if self.monitor_types and type_name not in self.monitor_types:
                continue
            if self.only_down and m.get("status") not in (8, 9):
                continue

            row = render_monitor(m, self.dashboard_url)
            fp = _hash(row)
            if self._changed(row["id"], fp):
                yield self._emit(row, fp)

    def _psp_rows(self) -> Iterator[dict[str, Any]]:
        for psp in self._walk_kind("public_status_pages"):
            row = render_psp(psp, self.dashboard_url)
            fp = _hash(row)
            if self._changed(row["id"], fp):
                yield self._emit(row, fp)

    def _alert_contact_rows(self) -> Iterator[dict[str, Any]]:
        for ac in self._walk_kind("alert_contacts"):
            row = render_alert_contact(ac, self.dashboard_url)
            fp = _hash(row)
            if self._changed(row["id"], fp):
                yield self._emit(row, fp)

    def _mwindow_rows(self) -> Iterator[dict[str, Any]]:
        for mw in self._walk_kind("maintenance_windows"):
            row = render_mwindow(mw, self.dashboard_url)
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
            except (UptimeRobotAPIError, UptimeRobotAccessError) as exc:
                self.stats["failed"] += 1
                logger.warning(
                    "UptimeRobot deletion check failed for one record, retried next run: %s",
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
            "monitor": "monitors",
            "psp": "public_status_pages",
            "alert_contact": "alert_contacts",
            "mwindow": "maintenance_windows",
        }
        kind = kind_map.get(prefix)
        if not kind or kind not in self.include:
            return True

        endpoint = _ENDPOINTS[kind]
        data_key = _PAYLOAD_KEYS[kind]
        param_key = (
            "monitors"
            if kind == "monitors"
            else "psps"
            if kind == "public_status_pages"
            else "alert_contacts"
            if kind == "alert_contacts"
            else "mwindows"
        )
        try:
            res = self._call(endpoint, body={param_key: record_id})
        except UptimeRobotNotFoundError:
            return True

        records = res.get(data_key) or []
        if not records:
            return True

        record = records[0]
        if kind == "monitors":
            type_name = MONITOR_TYPE_MAP.get(record.get("type", 1), "")
            if self.monitor_types and type_name not in self.monitor_types:
                return True
            if self.only_down and record.get("status") not in (8, 9):
                return True

        return False

    def rows(self) -> Iterator[dict[str, Any]]:
        yield from self._forget()
        if "monitors" in self.include:
            yield from self._monitor_rows()
        if "public_status_pages" in self.include:
            yield from self._psp_rows()
        if "alert_contacts" in self.include:
            yield from self._alert_contact_rows()
        if "maintenance_windows" in self.include:
            yield from self._mwindow_rows()
        self._close_cycle()
        yield from self._forget()


def _iter_rows(
    client: Any,
    dashboard_url: str,
    state: dict[str, Any],
    stats: dict[str, int],
    *,
    include: tuple[str, ...] = KINDS,
    monitor_types: list[str] | None = None,
    only_down: bool = False,
    max_requests: int = DEFAULT_MAX_REQUESTS,
    check_active: Callable[[], None] | None = None,
) -> Iterator[dict[str, Any]]:
    walker = _Walker(
        client,
        dashboard_url,
        state,
        stats,
        include=include,
        monitor_types=monitor_types,
        only_down=only_down,
        max_requests=max_requests,
        check_active=check_active,
    )
    try:
        yield from walker.rows()
    except _CutShortError as cut:
        stats["failed"] = max(1, stats.get("failed", 0))
        stats[f"failed_{cut.reason}"] = 1
        logger.warning(
            "UptimeRobot sync stopped early (%s); it resumes on the next run.",
            cut.reason,
        )


# ---------------------------------------------------------------------------
# Public factory
# ---------------------------------------------------------------------------
def uptimerobot_source(
    *,
    resource_name: str,
    api_key: str | None = None,
    base_url: str = DEFAULT_BASE_URL,
    dashboard_url: str = DEFAULT_DASHBOARD_URL,
    service: Any = None,
    check_active: Callable[[], None] | None = None,
    include: tuple[str, ...] = KINDS,
    monitor_types: list[str] | None = None,
    only_down: bool = False,
    max_requests: int = DEFAULT_MAX_REQUESTS,
):
    """Return a ``dlt`` resource yielding UptimeRobot uptime monitors for ``cognee.remember``.

    Args:
        resource_name: Stable connection identifier scoping sync state and tombstones.
        api_key: UptimeRobot API key.
        base_url: Base REST API URL (defaults to https://api.uptimerobot.com/v2).
        dashboard_url: Dashboard web URL for deep links (defaults to https://uptimerobot.com/dashboard).
        service: Injected client instance for offline test fixtures.
        check_active: Checkpoint callable invoked before each HTTP request.
        include: Subset of kinds: ("monitors", "public_status_pages",
            "alert_contacts", "maintenance_windows").
        monitor_types: Only sync monitors of these types (e.g. ["HTTP(s)", "Ping", "Port"]).
        only_down: If True, only ingests monitors currently reporting DOWN or SEEMS_DOWN.
        max_requests: Maximum request budget per run.

    Returns:
        A ``dlt`` resource configured with write_disposition="merge", primary_key="id",
        and a "_deleted" hard-delete marker.
    """
    import dlt

    if getattr(dlt_utils, "DOCUMENT_SYNC_VERSION", 0) < 1:
        raise RuntimeError(
            "UptimeRobot sync requires cognee>=1.6.3 (table-scoped document cleanup)."
        )
    if not isinstance(resource_name, str) or not resource_name.strip():
        raise ValueError("uptimerobot_source needs a valid resource_name")

    include = tuple(include)
    if not include or set(include) - set(KINDS):
        raise ValueError(f"include must be a non-empty subset of {KINDS}")

    client = (
        service
        if service is not None
        else UptimeRobotClient(api_key=api_key or "", base_url=base_url)
    )
    resolved_dash = (dashboard_url or DEFAULT_DASHBOARD_URL).rstrip("/")
    stats: dict[str, int] = {}

    @dlt.resource(
        name=resource_name,
        primary_key="id",
        write_disposition="merge",
        columns={"_deleted": {"data_type": "bool", "hard_delete": True}},
    )
    def uptimerobot_workspace():
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
            monitor_types=monitor_types,
            only_down=only_down,
            max_requests=max_requests,
            check_active=check_active,
        )
        yield from dlt_utils.guarded_rows(rows, check_active)

    res = uptimerobot_workspace()
    setattr(res, DOCUMENT_SOURCE_ATTR, "uptimerobot")
    setattr(res, dlt_utils.PIPELINE_SCOPE_ATTR, resource_name)
    res.cognee_sync_stats = stats
    return res
