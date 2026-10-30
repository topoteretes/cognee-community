"""Ahrefs connector for cognee: dated SEO rows for Domain Rating, backlinks,
organic keywords and Site Audit issues, one ``dlt`` source per report.

The rows take cognee's relational path. Every call goes through dlt's
``rest_api`` source. See the README for the sync and cost design.
"""

import contextlib
import os
import re
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any
from urllib.parse import urlparse

import requests
from cognee.shared.logging_utils import get_logger
from cognee.tasks.ingestion import dlt_utils

logger = get_logger("ahrefs_connector")

API_BASE_URL = "https://api.ahrefs.com/v3/"
# Every paid request costs at least this many units.
BASE_COST = 50
# Site Explorer requests for these domains are free test queries.
FREE_TARGETS = ("ahrefs.com", "wordcount.com")

REPORTS = ("domain_rating", "backlinks", "organic_keywords", "site_audit")
DEFAULT_REPORTS = ("domain_rating",)
TARGET_REPORTS = ("domain_rating", "backlinks", "organic_keywords")

# Only 1-unit fields. traffic, keyword_difficulty and volume cost 5 or 10 units a row.
DOMAIN_RATING_FIELDS = ("date", "domain_rating")
BACKLINK_FIELDS = (
    "url_from",
    "url_to",
    "anchor",
    "domain_rating_source",
    "is_dofollow",
    "first_seen_link",
    "last_visited",
    "is_lost",
)
KEYWORD_FIELDS = ("keyword", "keyword_country", "best_position", "best_position_url")

MODES = ("exact", "prefix", "domain", "subdomains")
HISTORY_GROUPINGS = ("daily", "weekly", "monthly")
DEFAULT_MAX_UNITS = 1000
DEFAULT_RESOURCE_NAME = "ahrefs"

# Retries for 429, 5xx and network errors. A longer Retry-After stops the run
# instead of sleeping, because cognee holds its dlt staging lock meanwhile.
MAX_ATTEMPTS = 4
MAX_RETRY_AFTER = 60.0
REQUEST_TIMEOUT = 60.0
_RETRY_STATUSES = (429, 500, 502, 503, 504)

# Table names are <resource_name>_<report>; the longest suffix has 17 characters
# and Postgres cuts identifiers at 63.
_RESOURCE_NAME_RE = re.compile(r"^[a-z][a-z0-9_]{0,39}$")


class AhrefsAPIError(RuntimeError):
    """Ahrefs answered with an error, or did not answer. Never holds the key."""

    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


class AhrefsRateLimitError(AhrefsAPIError):
    """Ahrefs kept answering 429, or asked to wait longer than ``MAX_RETRY_AFTER``."""


class AhrefsUnitsError(RuntimeError):
    """The API key or workspace has fewer units left than the sync may need."""


@dataclass(frozen=True)
class _Config:
    targets: tuple[str, ...]
    project_ids: tuple[str, ...]
    mode: str
    history_months: int
    history_grouping: str
    backlinks_limit: int
    keywords_limit: int
    country: str | None
    lost_lookback_days: int
    keep_snapshots: int


def _today() -> date:
    return date.today()


def _sleep(seconds: float) -> None:
    time.sleep(seconds)


# ---------------------------------------------------------------------------
# Transport: dlt rest_api with bounded retries
# ---------------------------------------------------------------------------
Fetch = Callable[[str, dict, str], list[dict]]


class _TimeoutSession(requests.Session):
    """A session that never waits forever on one request."""

    def send(self, request, **kwargs):
        kwargs["timeout"] = kwargs.get("timeout") or REQUEST_TIMEOUT
        return super().send(request, **kwargs)


def _make_fetch(
    api_key: str,
    session: Any,
    stats: dict,
    check_active: Callable[[], None] | None = None,
) -> Fetch:
    """Return ``fetch(path, params, data_selector) -> rows`` built on dlt's rest_api."""
    from dlt.sources.rest_api import rest_api_resources

    session = session if session is not None else _TimeoutSession()

    def record_cost(response, *_args, **_kwargs) -> None:
        stats["requests"] += 1
        with contextlib.suppress(TypeError, ValueError):
            stats["units_used"] += int(response.headers.get("x-api-units-cost-total-actual", 0))

    def request_once(path: str, params: dict, data_selector: str) -> list[dict]:
        (resource,) = rest_api_resources(
            {
                "client": {
                    "base_url": API_BASE_URL,
                    "auth": {"type": "bearer", "token": api_key},
                    "headers": {"Accept": "application/json"},
                    # Ahrefs answers in one page; a bigger result needs a bigger `limit`.
                    "paginator": "single_page",
                    "session": session,
                },
                "resources": [
                    {
                        "name": "ahrefs_request",
                        "endpoint": {
                            "path": path,
                            "params": {k: v for k, v in params.items() if v is not None},
                            "data_selector": data_selector,
                            "response_actions": [record_cost],
                        },
                    }
                ],
            }
        )
        return list(resource)

    def fetch(path: str, params: dict, data_selector: str) -> list[dict]:
        attempt = 0
        while True:
            attempt += 1
            if check_active is not None:
                check_active()
            try:
                return request_once(path, params, data_selector)
            except Exception as exc:
                response = _http_response(exc)
                if response is None and not _is_network_error(exc):
                    raise
                status = response.status_code if response is not None else None
                reason = _error_text(response, api_key)
                if status in (401, 403):
                    stats["failed_auth"] += 1
                if status is not None and status not in _RETRY_STATUSES:
                    raise AhrefsAPIError(
                        f"Ahrefs answered {status} for {path}{reason}.", status
                    ) from None
                delay = _retry_delay(response, attempt)
                too_long = delay > MAX_RETRY_AFTER
                if too_long or attempt >= MAX_ATTEMPTS:
                    what = f"answered {status}" if status else "did not answer"
                    when = f" and asked to wait {delay:.0f}s" if too_long else ""
                    tries = "" if too_long else f" after {attempt} attempts"
                    message = (
                        f"Ahrefs {what} for {path}{when}{tries}{reason}. "
                        "The sync stopped and changed nothing; run it again later."
                    )
                    if status == 429:
                        stats["failed_rate_limit"] += 1
                        raise AhrefsRateLimitError(message, status) from None
                    raise AhrefsAPIError(message, status) from None
                stats["retries"] += 1
                logger.warning(
                    "Ahrefs %s: %s, retrying in %.0fs (%d/%d).",
                    path,
                    status or "network error",
                    delay,
                    attempt,
                    MAX_ATTEMPTS,
                )
                _sleep(delay)

    return fetch


def _http_response(exc: BaseException) -> Any:
    """The HTTP response behind an error raised while dlt iterated a rest_api resource."""
    seen = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        response = getattr(current, "response", None)
        if response is not None and getattr(response, "status_code", None) is not None:
            return response
        current = current.__cause__ or current.__context__
    return None


def _is_network_error(exc: BaseException) -> bool:
    seen = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if isinstance(current, (requests.ConnectionError, requests.Timeout)):
            return True
        current = current.__cause__ or current.__context__
    return False


def _error_text(response: Any, api_key: str) -> str:
    """Ahrefs' own error message, short, with the key masked."""
    if response is None:
        return ""
    try:
        text = str(response.json().get("error") or "")
    except (ValueError, AttributeError):  # no JSON body, or not an object
        text = ""
    text = text.replace(api_key, "***")[:200]
    return f": {text}" if text else ""


def _retry_delay(response: Any, attempt: int) -> float:
    """Retry-After when Ahrefs sends it, else 2, 4, 8 seconds."""
    header = response.headers.get("Retry-After") if response is not None else None
    try:
        return max(0.0, float(header))
    except (TypeError, ValueError):
        return float(2**attempt)


def _check_units(fetch: Fetch, needed: int, stats: dict) -> None:
    """Refuse the sync before any paid request if the key cannot cover it."""
    usage = next(iter(fetch("subscription-info/limits-and-usage", {}, "limits_and_usage")), {})
    for owner, limit_key, used_key in (
        ("API key", "units_limit_api_key", "units_usage_api_key"),
        ("workspace", "units_limit_workspace", "units_usage_workspace"),
    ):
        limit = usage.get(limit_key)
        if limit is None:  # null means no limit
            continue
        left = int(limit) - int(usage.get(used_key) or 0)
        stats[f"units_left_{owner.replace(' ', '_').lower()}"] = left
        if left < needed:
            raise AhrefsUnitsError(
                f"The Ahrefs {owner} has {left} API units left this billing month, and this "
                f"sync can use up to {needed}. Nothing was requested. Lower the limits, select "
                "fewer reports or wait for the usage reset."
            )


# ---------------------------------------------------------------------------
# Cost estimate (worst case, before any request)
# ---------------------------------------------------------------------------
def _is_free(target: str) -> bool:
    host = urlparse(target if "://" in target else f"//{target}").hostname or ""
    return any(host == free or host.endswith(f".{free}") for free in FREE_TARGETS)


def _request_cost(fields: int, rows: int) -> int:
    return max(BASE_COST, fields * rows)


def _estimate(report: str, config: _Config) -> int:
    """Most units one sync of ``report`` can use. Free test targets cost nothing."""
    if report == "site_audit":
        return BASE_COST * len(config.project_ids)
    paid = [t for t in config.targets if not _is_free(t)]
    if report == "domain_rating":
        periods = {
            "daily": config.history_months * 31,
            "weekly": config.history_months * 31 // 7,
            "monthly": config.history_months,
        }[config.history_grouping] + 1
        return len(paid) * _request_cost(len(DOMAIN_RATING_FIELDS), periods)
    if report == "backlinks":
        return len(paid) * _request_cost(len(BACKLINK_FIELDS), config.backlinks_limit)
    return len(paid) * _request_cost(len(KEYWORD_FIELDS), config.keywords_limit)


# ---------------------------------------------------------------------------
# Sync state machine (pure given a fetch + state dict — unit-testable)
# ---------------------------------------------------------------------------
def _new_stats(estimate: int) -> dict[str, int]:
    return {
        "requests": 0,
        "retries": 0,
        "units_estimated": estimate,
        "units_used": 0,
        "rows": 0,
        "deleted": 0,
        "truncated": 0,
        "failed_auth": 0,
        "failed_rate_limit": 0,
    }


def _iter_rows(
    report: str,
    fetch: Fetch,
    config: _Config,
    state: dict,
    stats: dict,
    estimate: int,
) -> Iterator[dict]:
    """Yield the changed rows of one report, plus hard-delete tombstones.

    ``state`` is a plain dict standing in for dlt's resource state.  Every
    request happens before the first yield, and the state is only written
    after the last one, so a failed run changes nothing.
    """
    stats.clear()
    stats.update(_new_stats(estimate))

    old_scopes: dict[str, dict] = dict(state.get("scopes", {}))
    active = config.project_ids if report == "site_audit" else config.targets
    rows: list[dict] = []
    deletes: list[str] = []

    # Targets or projects dropped from the config are out of scope now.
    for scope, scope_state in old_scopes.items():
        if scope not in active:
            deletes.extend(_all_ids(scope_state))

    if estimate:
        _check_units(fetch, estimate, stats)

    sync = _SYNC[report]
    today = _today()
    new_scopes: dict[str, dict] = {}
    for scope in active:
        result = sync(fetch, config, scope, old_scopes.get(scope, {}), today, rows, deletes, stats)
        if result is not None:
            new_scopes[scope] = result

    stats["rows"] = len(rows)
    stats["deleted"] = len(deletes)
    for row_id in deletes:
        yield {"id": row_id, "_deleted": True}
    yield from rows

    state["scopes"] = new_scopes
    logger.info("Ahrefs %s: sync stats %s", report, stats)


def _all_ids(scope_state: dict) -> list[str]:
    ids = set(scope_state.get("known", []))
    for snapshot_ids in scope_state.get("snapshots", {}).values():
        ids.update(snapshot_ids)
    return sorted(ids)


def _sync_domain_rating(fetch, config, target, scope_state, today, rows, deletes, stats):
    last_date = scope_state.get("last_date")
    # Read again from the last stored date: the open month (or week, or day)
    # keeps changing until it closes.
    date_from = last_date or _months_ago(today, config.history_months).isoformat()
    items = fetch(
        "site-explorer/domain-rating-history",
        {
            "target": target,
            "date_from": date_from,
            "date_to": today.isoformat(),
            "history_grouping": config.history_grouping,
        },
        "domain_ratings",
    )
    known = set(scope_state.get("known", []))
    for item in items:
        day = str(item.get("date") or "")[:10]
        if not day or item.get("domain_rating") is None:
            continue
        row_id = f"{target}|{day}"
        known.add(row_id)
        last_date = max(last_date or day, day)
        rows.append(
            {
                "id": row_id,
                "target": target,
                "date": day,
                # Always a float, so 71 and 71.5 land in the same column.
                "domain_rating": float(item["domain_rating"]),
                "_deleted": False,
            }
        )
    return {"known": sorted(known), "last_date": last_date}


def _sync_backlinks(fetch, config, target, scope_state, today, rows, deletes, stats):
    # Read lost links back to the last sync at least, so a long pause between
    # syncs cannot hide a lost link.
    since = (today - timedelta(days=config.lost_lookback_days)).isoformat()
    since = min(since, scope_state.get("last_synced") or since)
    items = fetch(
        "site-explorer/all-backlinks",
        {
            "target": target,
            "mode": config.mode,
            "select": ",".join(BACKLINK_FIELDS),
            "limit": config.backlinks_limit,
            "aggregation": "all",
            # Live links plus the links lost since then, flagged is_lost.
            "history": f"since:{since}",
            "order_by": "domain_rating_source:desc",
        },
        "backlinks",
    )
    if len(items) >= config.backlinks_limit:
        stats["truncated"] += 1

    # The same link can appear more than once on a page. Live wins over lost.
    links: dict[str, dict] = {}
    for item in items:
        if not item.get("url_from") or not item.get("url_to"):
            continue
        row_id = f"{target}|{item['url_from']}|{item['url_to']}|{item.get('anchor') or ''}"
        if row_id not in links or links[row_id].get("is_lost"):
            links[row_id] = item

    known = set(scope_state.get("known", []))
    for row_id, item in links.items():
        if item.get("is_lost"):
            # Ahrefs confirms the link is gone. A link we never stored has
            # nothing to forget.
            if row_id in known:
                known.discard(row_id)
                deletes.append(row_id)
            continue
        known.add(row_id)
        rows.append(
            {
                "id": row_id,
                "target": target,
                "url_from": item["url_from"],
                "url_to": item["url_to"],
                "anchor": item.get("anchor") or "",
                "domain_rating_source": _float(item.get("domain_rating_source")),
                "is_dofollow": bool(item.get("is_dofollow")),
                "first_seen_link": item.get("first_seen_link"),
                "last_visited": item.get("last_visited"),
                "_deleted": False,
            }
        )
    return {"known": sorted(known), "last_synced": today.isoformat()}


def _sync_organic_keywords(fetch, config, target, scope_state, today, rows, deletes, stats):
    snapshot = today.isoformat()
    items = fetch(
        "site-explorer/organic-keywords",
        {
            "target": target,
            "mode": config.mode,
            "date": snapshot,
            "country": config.country,
            "select": ",".join(KEYWORD_FIELDS),
            "limit": config.keywords_limit,
            "order_by": "best_position:asc",
        },
        "keywords",
    )
    if len(items) >= config.keywords_limit:
        stats["truncated"] += 1

    snapshot_rows: dict[str, dict] = {}
    for item in items:
        keyword = item.get("keyword")
        if not keyword:
            continue
        country = item.get("keyword_country") or config.country or ""
        row_id = f"{target}|{snapshot}|{country}|{keyword}"
        snapshot_rows[row_id] = {
            "id": row_id,
            "target": target,
            "snapshot_date": snapshot,
            "country": country,
            "keyword": keyword,
            "position": item.get("best_position"),
            "url": item.get("best_position_url"),
            "_deleted": False,
        }
    return _store_snapshot(config, scope_state, snapshot, snapshot_rows, rows, deletes)


def _sync_site_audit(fetch, config, project_id, scope_state, today, rows, deletes, stats):
    snapshot = today.isoformat()
    try:
        items = fetch("site-audit/issues", {"project_id": project_id}, "issues")
    except AhrefsAPIError as exc:
        if exc.status != 404:
            raise
        # The project was deleted or this key cannot see it any more.
        logger.warning("Ahrefs Site Audit: a project was not found; forgetting its issues.")
        deletes.extend(_all_ids(scope_state))
        return None

    snapshot_rows: dict[str, dict] = {}
    for item in items:
        if not item.get("issue_id") or not item.get("crawled"):
            continue  # no URL has this issue
        row_id = f"{project_id}|{snapshot}|{item['issue_id']}"
        snapshot_rows[row_id] = {
            "id": row_id,
            "project_id": project_id,
            "snapshot_date": snapshot,
            "issue_id": item["issue_id"],
            "issue": item.get("name") or "",
            "importance": item.get("importance") or "",
            "category": item.get("category") or "",
            "urls_affected": int(item["crawled"]),
            "change": item.get("change"),
            "_deleted": False,
        }
    return _store_snapshot(config, scope_state, snapshot, snapshot_rows, rows, deletes)


def _store_snapshot(config, scope_state, snapshot, snapshot_rows, rows, deletes):
    """Keep today's snapshot and the ``keep_snapshots - 1`` before it."""
    snapshots: dict[str, list[str]] = {
        day: list(ids) for day, ids in scope_state.get("snapshots", {}).items()
    }
    # A second sync on the same day replaces that day's snapshot.
    deletes.extend(sorted(set(snapshots.get(snapshot, [])) - set(snapshot_rows)))
    if snapshot_rows:
        snapshots[snapshot] = sorted(snapshot_rows)
    else:
        snapshots.pop(snapshot, None)
    rows.extend(snapshot_rows.values())
    for day in sorted(snapshots)[: -config.keep_snapshots]:
        deletes.extend(snapshots.pop(day))
    return {"snapshots": snapshots}


_SYNC = {
    "domain_rating": _sync_domain_rating,
    "backlinks": _sync_backlinks,
    "organic_keywords": _sync_organic_keywords,
    "site_audit": _sync_site_audit,
}


def _months_ago(day: date, months: int) -> date:
    month_index = day.year * 12 + day.month - 1 - months
    return date(month_index // 12, month_index % 12 + 1, 1)


def _float(value: Any) -> float | None:
    return float(value) if value is not None else None


# ---------------------------------------------------------------------------
# Public factory
# ---------------------------------------------------------------------------
def ahrefs_source(
    targets: list[str] | str | None = None,
    *,
    reports: list[str] | str | None = None,
    project_ids: list[int | str] | int | str | None = None,
    api_key: str | None = None,
    max_units: int = DEFAULT_MAX_UNITS,
    mode: str = "subdomains",
    history_months: int = 12,
    history_grouping: str = "monthly",
    backlinks_limit: int = 50,
    keywords_limit: int = 50,
    country: str | None = None,
    lost_lookback_days: int = 90,
    keep_snapshots: int = 12,
    resource_name: str = DEFAULT_RESOURCE_NAME,
    check_active: Callable[[], None] | None = None,
    session: Any = None,
    service: Any = None,
) -> list:
    """Return one ``dlt`` source per selected report.

    Hand the list to ``cognee.remember(...)`` with ``write_disposition="merge"``,
    ``primary_key="id"``, ``max_rows_per_table=0``, ``incremental_loading=False``
    and ``data_cache=False``.

    Args:
        targets: Domains or URLs for the Site Explorer reports, like ``["example.com"]``.
        reports: Any of ``domain_rating`` (default), ``backlinks``,
            ``organic_keywords`` and ``site_audit``.  Every report costs units.
        project_ids: Site Audit project ids, from the project URL in Ahrefs.
        api_key: Ahrefs API key.  Falls back to ``AHREFS_API_KEY``.
        max_units: Most units one sync may use, worst case.  Building the
            sources fails above it, before any request.
        mode: Target scope: ``exact``, ``prefix``, ``domain`` or ``subdomains``.
        history_months: Domain Rating history read on the first sync.
        history_grouping: ``daily``, ``weekly`` or ``monthly`` Domain Rating points.
        backlinks_limit: Strongest backlinks tracked per target.
        keywords_limit: Best-ranked keywords kept per target and snapshot.
        country: Two-letter country for keyword rankings.  All countries when omitted.
        lost_lookback_days: How far back lost backlinks are read, at least back
            to the last sync.
        keep_snapshots: Keyword and Site Audit snapshots kept per target or project.
        resource_name: Prefix of the table, source and sync state names
            (``<resource_name>_<report>``).  Give each Ahrefs account or
            configuration its own name when several sync into one dataset.
        check_active: Optional host authorization checkpoint, called before
            every request and around every row.
        session: A ``requests`` session for the API, mainly for tests.
            ``service`` is an alias.
    """
    import dlt

    if session is not None and service is not None and session is not service:
        raise ValueError("Pass session= or service=, not both.")
    session = session if session is not None else service

    selected = _normalize_reports(reports)
    config = _Config(
        targets=_normalize_list(targets),
        project_ids=_normalize_list(project_ids),
        mode=_one_of("mode", mode, MODES),
        history_months=_at_least("history_months", history_months, 1),
        history_grouping=_one_of("history_grouping", history_grouping, HISTORY_GROUPINGS),
        backlinks_limit=_at_least("backlinks_limit", backlinks_limit, 1),
        keywords_limit=_at_least("keywords_limit", keywords_limit, 1),
        country=country.strip().lower() if country and country.strip() else None,
        lost_lookback_days=_at_least("lost_lookback_days", lost_lookback_days, 1),
        keep_snapshots=_at_least("keep_snapshots", keep_snapshots, 1),
    )
    _at_least("max_units", max_units, 0)
    if not isinstance(resource_name, str) or not _RESOURCE_NAME_RE.match(resource_name):
        raise ValueError(
            "resource_name must start with a letter, use only a-z, 0-9 and _, "
            "and have at most 40 characters."
        )
    if any(r in TARGET_REPORTS for r in selected) and not config.targets:
        # An empty list would forget every synced row, so refuse it.
        raise ValueError('targets is empty. Pass domains like ["example.com"].')
    if "site_audit" in selected and not config.project_ids:
        raise ValueError("The site_audit report needs project_ids from your Site Audit projects.")

    resolved_key = api_key or os.environ.get("AHREFS_API_KEY")
    if not resolved_key:
        # Fail when the sources are built, not halfway through a pipeline run.
        raise ValueError("Ahrefs API key required: pass api_key= or set AHREFS_API_KEY.")

    estimates = {report: _estimate(report, config) for report in selected}
    if sum(estimates.values()) > max_units:
        detail = ", ".join(f"{report} {units}" for report, units in estimates.items())
        raise ValueError(
            f"This sync can use up to {sum(estimates.values())} Ahrefs API units "
            f"({detail}), more than max_units={max_units}. Raise max_units, lower the "
            "limits or select fewer reports."
        )

    return [
        _report_source(
            dlt,
            report,
            resource_name,
            config,
            resolved_key,
            estimates[report],
            session,
            check_active,
        )
        for report in selected
    ]


def _report_source(dlt, report, resource_name, config, api_key, estimate, session, check_active):
    table_name = f"{resource_name}_{report}"
    stats: dict[str, int] = _new_stats(estimate)

    @dlt.resource(
        name=table_name,
        write_disposition="merge",
        primary_key="id",
        # `_deleted` is a boolean hard-delete marker: rows where it is True are
        # removed from the dlt destination on merge, and cognee then drops them
        # from the graph and vector stores.
        columns={"_deleted": {"data_type": "bool", "hard_delete": True}},
    )
    def report_rows():
        fetch = _make_fetch(api_key, session, stats, check_active)
        yield from dlt_utils.guarded_rows(
            _iter_rows(report, fetch, config, dlt.current.resource_state(), stats, estimate),
            check_active,
        )

    @dlt.source(name=table_name)
    def ahrefs_report():
        return report_rows

    source = ahrefs_report()
    # The pipeline scope, the source, the resource and the table share one name,
    # so this report's dlt state stays apart from every other source.
    setattr(source, dlt_utils.PIPELINE_SCOPE_ATTR, table_name)
    # Host-readable diagnostics contain counts only, never targets, rows or the key.
    source.cognee_sync_stats = stats
    source.resources[table_name].cognee_sync_stats = stats
    return source


def _normalize_reports(reports: list[str] | str | None) -> tuple[str, ...]:
    if reports is None:
        return DEFAULT_REPORTS
    if isinstance(reports, str):
        reports = [reports]
    cleaned = tuple(dict.fromkeys(r.strip().lower() for r in reports if r.strip()))
    unknown = [r for r in cleaned if r not in REPORTS]
    if unknown:
        raise ValueError(f"Unknown Ahrefs report {unknown[0]!r}. Choose from {', '.join(REPORTS)}.")
    if not cleaned:
        raise ValueError(f"reports is empty. Choose from {', '.join(REPORTS)}.")
    return cleaned


def _normalize_list(values: Any) -> tuple[str, ...]:
    if values is None:
        return ()
    if isinstance(values, (str, int)):
        values = [values]
    return tuple(dict.fromkeys(str(v).strip() for v in values if str(v).strip()))


def _one_of(field: str, value: str, allowed: tuple[str, ...]) -> str:
    if value not in allowed:
        raise ValueError(f"{field} must be one of {', '.join(allowed)}, not {value!r}.")
    return value


def _at_least(field: str, value: int, minimum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise ValueError(f"{field} must be an integer of at least {minimum}.")
    return value
