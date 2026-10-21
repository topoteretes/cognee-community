"""Ahrefs connector for cognee — ``dlt`` sources that keep dated SEO data in memory.

Sync Domain Rating history, backlinks, organic keyword rankings and Site Audit
issues into cognee.  The sources produced here are handed directly to
:func:`cognee.remember`::

    import cognee
    from cognee_community_connector_ahrefs import ahrefs_source

    await cognee.remember(
        ahrefs_source(targets=["example.com"], reports=["domain_rating", "backlinks"]),
        dataset_name="seo",
        primary_key="id",
        write_disposition="merge",   # incremental upserts + forget-on-delete
        max_rows_per_table=0,        # 0 = no row cap (reports exceed the default 50)
    )

Design
------
* **One source per report** — ``ahrefs_source`` returns one ``dlt`` source per
  selected report, so each report is its own cognee data item.  Syncing Domain
  Rating does not rebuild the backlinks in memory, and a report that returns
  nothing new leaves its data as it is.
* **Relational path** — rows are SEO metrics, not prose, so no document-source
  marker is set.  cognee builds the graph from the rows without an LLM, and
  every ISO date cell becomes a ``Timestamp`` edge, so trends stay queryable.
* **Transport** — every call goes through dlt's declarative ``rest_api``
  source: Bearer auth, one page per request (Ahrefs has no page cursor), and
  dlt's retries for 429 and 5xx with ``Retry-After``.
* **Cost** — the API is billed in units.  Only Domain Rating is on by default.
  The worst-case cost of a sync is checked against ``max_units`` when the
  sources are built, then against the units left on the key (a free endpoint)
  before the first paid request.  Each request asks only for 1-unit fields,
  and the units Ahrefs reports as spent are kept in ``cognee_sync_stats``.
* **Sync per report**
  - Domain Rating: a dated series.  The first run reads ``history_months``;
    later runs read from the last stored date, so the open period is refreshed.
  - Backlinks: the strongest ``backlinks_limit`` links, with links lost in the
    last ``lost_lookback_days``.  The list is limited, so a link that is
    missing from it is never forgotten.  Only a link Ahrefs marks as lost is.
  - Organic keywords and Site Audit issues: one dated snapshot per day.  The
    last ``keep_snapshots`` snapshots are kept; older ones are forgotten.
* **Forget-on-delete** — lost backlinks, snapshots past retention, targets or
  projects removed from the config, and a Site Audit project that answers 404
  are emitted with the ``_deleted`` hard-delete marker.  cognee rebuilds the
  report from staging and drops the rest from the graph and vector stores.
* **Errors never delete** — 401, 403, 429 and 5xx raise after dlt's retries,
  and dlt keeps the previous state and rows.

Limitations
-----------
* Backlinks outside the strongest ``backlinks_limit`` are not tracked; a link
  that drops out of that list stays as it was last seen.
* If every row of a report is forgotten, cognee keeps the last data of that
  report (its relational path does not delete on an empty read).  Forget the
  data item or the dataset in that case.
"""

import contextlib
import os
import re
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any
from urllib.parse import urlparse

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

_NAME_RE = re.compile(r"^[a-z][a-z0-9_]*$")


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


# ---------------------------------------------------------------------------
# Transport: dlt rest_api
# ---------------------------------------------------------------------------
Fetch = Callable[[str, dict, str], list[dict]]


def _make_fetch(api_key: str, session: Any, stats: dict) -> Fetch:
    """Return ``fetch(path, params, data_selector) -> rows`` built on dlt's rest_api."""
    from dlt.sources.rest_api import rest_api_resources

    def record_cost(response, *_args, **_kwargs) -> None:
        stats["requests"] += 1
        with contextlib.suppress(TypeError, ValueError):
            stats["units_used"] += int(response.headers.get("x-api-units-cost-total-actual", 0))

    def fetch(path: str, params: dict, data_selector: str) -> list[dict]:
        client: dict[str, Any] = {
            "base_url": API_BASE_URL,
            "auth": {"type": "bearer", "token": api_key},
            "headers": {"Accept": "application/json"},
            # Ahrefs answers in one page; a bigger result needs a bigger `limit`.
            "paginator": "single_page",
        }
        if session is not None:
            client["session"] = session
        (resource,) = rest_api_resources(
            {
                "client": client,
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

    return fetch


def _http_status(exc: BaseException) -> int | None:
    """HTTP status behind an error raised while dlt iterated a rest_api resource."""
    seen = set()
    while exc is not None and id(exc) not in seen:
        seen.add(id(exc))
        status = getattr(getattr(exc, "response", None), "status_code", None)
        if status is not None:
            return int(status)
        exc = exc.__cause__ or exc.__context__
    return None


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
    stats.update(requests=0, units_estimated=estimate, units_used=0, rows=0, deleted=0, truncated=0)

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
    since = (today - timedelta(days=config.lost_lookback_days)).isoformat()
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
        logger.info(
            "Ahrefs backlinks: the result hit backlinks_limit=%d; links outside it are kept "
            "as last seen, never forgotten.",
            config.backlinks_limit,
        )

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
    return {"known": sorted(known)}


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
    except Exception as exc:
        if _http_status(exc) != 404:
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
    name: str = "ahrefs",
    check_active: Callable[[], None] | None = None,
    session: Any = None,
) -> list:
    """Return one ``dlt`` source per selected report.

    Hand the list to ``cognee.remember(...)`` with ``write_disposition="merge"``,
    ``primary_key="id"`` and ``max_rows_per_table=0``.

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
        lost_lookback_days: How far back lost backlinks are read.  Sync more
            often than this, or a lost link may be missed.
        keep_snapshots: Keyword and Site Audit snapshots kept per target or project.
        name: Prefix of the source and table names.  Give each Ahrefs account
            its own name when several sync into one dataset.
        check_active: Optional host authorization checkpoint during extraction.
        session: A ``requests`` session for the API, mainly for tests.  Without
            it, dlt's session retries 429 and 5xx.
    """
    import dlt

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
    if not _NAME_RE.match(name):
        raise ValueError("name must start with a letter and use only a-z, 0-9 and _.")
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
            dlt, report, name, config, resolved_key, estimates[report], session, check_active
        )
        for report in selected
    ]


def _report_source(dlt, report, name, config, api_key, estimate, session, check_active):
    table_name = f"{name}_{report}"
    stats: dict[str, int] = {}

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
        fetch = _make_fetch(api_key, session, stats)
        yield from dlt_utils.guarded_rows(
            _iter_rows(report, fetch, config, dlt.current.resource_state(), stats, estimate),
            check_active,
        )

    @dlt.source(name=table_name)
    def ahrefs_report():
        return report_rows

    source = ahrefs_report()
    # Keeps this report's dlt state apart from other sources in the dataset.
    setattr(source, dlt_utils.PIPELINE_SCOPE_ATTR, table_name)
    # Host-readable diagnostics contain counts only, never targets or rows.
    source.cognee_sync_stats = stats
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
    if not isinstance(value, int) or value < minimum:
        raise ValueError(f"{field} must be an integer of at least {minimum}.")
    return value
