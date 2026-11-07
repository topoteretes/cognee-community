"""PostHog connector for cognee: a ``dlt`` source that turns product analytics into memory.

One document per saved insight (with its latest cached result), dashboard, cohort,
feature flag and experiment, synced incrementally with forget-on-delete. Hand the
source to :func:`cognee.remember` with ``write_disposition="merge"``::

    await cognee.remember(
        posthog_source(project_id="12345", api_key=os.environ["POSTHOG_API_KEY"]),
        dataset_name="posthog",
        write_disposition="merge",
    )

The README describes what is stored, how changes and deletions are found, and what a
run does when PostHog fails part of the way through.
"""

import hashlib
import json
import re
import time
from collections.abc import Callable, Iterator, Mapping, Sequence
from typing import Any
from urllib.parse import quote, urlsplit

import dlt
import requests
from cognee.shared.logging_utils import get_logger
from cognee.tasks.ingestion import dlt_utils
from cognee.tasks.ingestion.dlt_utils import DOCUMENT_SOURCE_ATTR
from dlt.common.normalizers.naming.snake_case import NamingConvention

logger = get_logger("posthog_connector")

POSTHOG_SOURCE_NAME = "posthog"
DEFAULT_HOST = "https://us.posthog.com"

# Kind -> (API path segment, label, UI path segment).
KINDS: dict[str, tuple[str, str, str]] = {
    "insights": ("insights", "insight", "insights"),
    "dashboards": ("dashboards", "dashboard", "dashboard"),
    "cohorts": ("cohorts", "cohort", "cohorts"),
    "feature_flags": ("feature_flags", "feature flag", "feature_flags"),
    "experiments": ("experiments", "experiment", "experiments"),
}
DEFAULT_KINDS: tuple[str, ...] = tuple(KINDS)

_LIST_PARAMS: dict[str, dict[str, Any]] = {
    # Saved insights only, with whatever result PostHog has cached: force_cache
    # never starts a calculation.
    "insights": {"saved": "true", "refresh": "force_cache"},
}
_PAGE_SIZE = 100
_MAX_PAGES = 10_000
_MAX_ATTEMPTS = 3
_MAX_RETRY_DELAY_SECONDS = 60.0
# Retries sleep inside cognee's staging lock, which every other dlt ingestion in
# the process waits on, so a run waits at most this long in total.
_WAIT_BUDGET_SECONDS = 60.0
_REQUEST_TIMEOUT_SECONDS = 60
_MAX_RESULT_LINES = 10
_MAX_SQL_CHARS = 1_000

_STAT_KEYS = (
    "scanned",
    "skipped",
    "changed",
    "deleted",
    "failed",
    "failed_auth",
    "failed_rate_limit",
    "requests",
    "complete",
)

_TOMBSTONE_COLUMNS = {"_deleted": {"data_type": "bool", "hard_delete": True}}
# C0/C1 control characters (tab and newline kept) and lone surrogates: a NUL makes
# Postgres reject the load and a lone surrogate fails the dlt extract.
_UNSAFE_CHARS = re.compile("[\x00-\x08\x0b-\x1f\x7f-\x9f\ud800-\udfff]")


class PostHogSyncError(RuntimeError):
    """The run cannot be trusted (stuck paging, an emptied project, a bad response)."""


# ---------------------------------------------------------------------------
# Public factory
# ---------------------------------------------------------------------------
def posthog_source(
    project_id: str | int,
    api_key: str | None = None,
    host: str = DEFAULT_HOST,
    kinds: Sequence[str] = DEFAULT_KINDS,
    service: Any = None,
    *,
    resource_name: str | None = None,
    check_active: Callable[[], None] | None = None,
    session: Any = None,
):
    """Create a dlt source that yields a PostHog project's curated objects as documents.

    Args:
        project_id: The PostHog project id (the number in ``/project/<id>``).
        api_key: Personal API key with read scopes for the selected kinds.
        host: ``https://us.posthog.com`` (default), ``https://eu.posthog.com`` or a
            self-hosted URL.
        kinds: What to sync, a list of any of :data:`DEFAULT_KINDS`.
        service: Pre-built ``requests``-compatible session (tests, proxies). It is
            used as is and not closed; when omitted, each run opens and closes its own.
        resource_name: dlt resource (table) name, which also scopes the sync state.
            Defaults to ``posthog_<project_id>``, so two projects synced into one
            dataset keep apart; give each host its own name if two hosts share a
            project id.
        check_active: Optional host checkpoint, called before every request and before
            every row is published; raising from it stops the run.
        session: Alias of ``service``. Give at most one.

    Returns:
        A dlt source for ``cognee.remember(..., write_disposition="merge")``. After a
        run, ``source.cognee_sync_stats`` holds counts only: ``scanned``, ``skipped``
        (unchanged), ``changed``, ``deleted``, ``failed`` (objects without an id),
        ``failed_auth`` and ``failed_rate_limit`` (1 when the run stopped for that
        reason), ``requests``, and ``complete`` (1 once the run finished).
    """
    if getattr(dlt_utils, "DOCUMENT_SYNC_VERSION", 0) < 1:
        raise RuntimeError(
            "The PostHog connector needs cognee's document sync cleanup (cognee>=1.6.3)."
        )
    project = str(project_id if project_id is not None else "").strip()
    if not project.isdigit():
        raise ValueError("project_id must be the PostHog project number, e.g. 12345.")
    base = str(host or "").strip().rstrip("/")
    if not base.startswith(("https://", "http://")):
        raise ValueError("host must be a URL such as https://us.posthog.com.")
    if isinstance(kinds, str) or not all(isinstance(kind, str) for kind in kinds):
        raise ValueError(f"kinds must be a list of kind names from {list(KINDS)}.")
    unknown = sorted(set(kinds) - set(KINDS))
    if unknown or not kinds:
        raise ValueError(f"kinds must be a non-empty selection of {list(KINDS)}; got {unknown}.")
    name = resource_name if resource_name is not None else f"{POSTHOG_SOURCE_NAME}_{project}"
    _check_resource_name(name)
    if service is not None and session is not None:
        raise ValueError("Pass service= or session=, not both.")
    client = service if service is not None else session
    secret = _check_key(api_key)
    if client is None and not secret:
        raise ValueError("api_key is required (a PostHog personal API key).")
    selection = tuple(kind for kind in KINDS if kind in kinds)
    stats: dict[str, int] = {}

    @dlt.resource(
        name=name,
        primary_key="id",
        write_disposition="merge",
        columns=_TOMBSTONE_COLUMNS,
    )
    def posthog_documents():
        http = client if client is not None else _make_session(secret)
        try:
            api = PostHogAPI(http, base, project, check_active=check_active, stats=stats)
            state = dlt.current.resource_state()
            yield from sync_project(api, selection, state, stats=stats, check_active=check_active)
        finally:
            if client is None:
                http.close()

    @dlt.source(name=POSTHOG_SOURCE_NAME)
    def _posthog():
        return posthog_documents

    source = _posthog()
    # Opt into the document ingestion path (row -> text document -> cognify).
    # resolve_dlt_sources reads this marker; it never imports this connector.
    setattr(source, DOCUMENT_SOURCE_ATTR, POSTHOG_SOURCE_NAME)
    # The table name also scopes the dlt state (document hashes), so the state and
    # the table it describes always resolve to the same pipeline.
    setattr(source, dlt_utils.PIPELINE_SCOPE_ATTR, name)
    source.cognee_sync_stats = stats
    return source


def _check_resource_name(name: Any) -> None:
    """A non-empty name of at most 64 characters that dlt keeps as it is.

    It names the table and scopes the state: two names dlt normalizes alike
    ("PostHog-1", "posthog_1") would share a table.
    """
    if not isinstance(name, str) or not name or len(name) > 64:
        raise ValueError("resource_name must be a non-empty string of at most 64 characters.")
    if NamingConvention().normalize_table_identifier(name) != name:
        raise ValueError("resource_name must be lowercase snake_case (letters, digits, _).")


def _check_key(key: str | None) -> str:
    """Strip a pasted key and reject a malformed one without echoing it."""
    secret = (key or "").strip()
    if secret and (not secret.isascii() or not secret.isprintable() or " " in secret):
        raise ValueError("The PostHog API key contains spaces or control characters.")
    return secret


def _make_session(api_key: str) -> Any:
    session = requests.Session()
    session.headers.update({"Accept": "application/json", "Authorization": f"Bearer {api_key}"})
    return session


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------
class PostHogAPI:
    """Thin read-only client for a project's REST endpoints: retries and paging."""

    def __init__(
        self,
        session: Any,
        host: str,
        project_id: str,
        check_active: Callable[[], None] | None = None,
        stats: dict | None = None,
    ):
        self.session = session
        self.host = host.rstrip("/")
        self.project_id = str(project_id)
        self.check_active = check_active
        self.stats = stats if stats is not None else {}
        self.waited = 0.0  # seconds slept on retries this run

    def get(self, url: str, params: Any = None) -> Any:
        """GET a URL, retrying 429/5xx and network errors within the wait budget."""
        path = urlsplit(url).path
        for attempt in range(_MAX_ATTEMPTS):
            if self.check_active is not None:
                self.check_active()
            self.stats["requests"] = self.stats.get("requests", 0) + 1
            failure: Exception | None = None
            try:
                response = self.session.request(
                    "GET", url, params=params, timeout=_REQUEST_TIMEOUT_SECONDS
                )
            except (requests.ConnectionError, requests.Timeout) as exc:
                failure, reason = exc, type(exc).__name__
                delay = min(2.0**attempt, _MAX_RETRY_DELAY_SECONDS)
            else:
                status = response.status_code
                if status != 429 and status < 500:
                    if status in (401, 403):
                        self.stats["failed_auth"] = 1
                    return _parse(response, path)
                reason, delay = f"HTTP {status}", _retry_delay(response.headers, attempt)
            if attempt == _MAX_ATTEMPTS - 1 or self.waited + delay > _WAIT_BUDGET_SECONDS:
                break
            logger.warning("PostHog: %s on %s; retrying in %.1fs.", reason, path, delay)
            self.waited += delay
            time.sleep(delay)
        if failure is not None:
            raise failure
        if response.status_code == 429:
            self.stats["failed_rate_limit"] = 1
            raise PostHogSyncError(
                f"PostHog is rate-limiting {path} (HTTP 429). The run stopped before "
                "changing anything; the next run starts over."
            )
        raise PostHogSyncError(
            f"PostHog failed {path} with HTTP {response.status_code}. The run stopped "
            "before changing anything; the next run starts over."
        )

    def list(self, kind: str) -> Iterator[dict]:
        """Yield every object of ``kind`` in the project, following ``next`` links."""
        segment = KINDS[kind][0]
        url: str | None = f"{self.host}/api/projects/{quote(self.project_id, safe='')}/{segment}/"
        params: dict[str, Any] | None = {"limit": _PAGE_SIZE, **_LIST_PARAMS.get(kind, {})}
        seen: set[str] = set()
        for _ in range(_MAX_PAGES):
            page = self.get(url, params)
            yield from page.get("results") or []
            next_url = page.get("next")
            if not next_url:
                return
            if next_url in seen or next_url == url:
                # A listing that cannot move forward must not look finished.
                raise PostHogSyncError(f"PostHog returned the same {kind} page link twice.")
            seen.add(next_url)
            url, params = next_url, None  # the next link carries the query string
        raise PostHogSyncError(f"PostHog listing of {kind} exceeded {_MAX_PAGES} pages.")


def _parse(response: Any, path: str) -> Any:
    """Raise on a failed response with a hint for the common causes; else decode JSON."""
    if response.status_code in (401, 403):
        raise requests.HTTPError(
            f"PostHog refused {path} (HTTP {response.status_code}). Check that the personal "
            "API key is valid, has read scopes for the selected kinds, and can access the "
            "project.",
            response=response,
        )
    if response.status_code == 404:
        raise PostHogSyncError(
            f"PostHog has nothing at {path} (HTTP 404). Check project_id and host."
        )
    response.raise_for_status()
    try:
        return response.json()
    except ValueError:
        raise PostHogSyncError(
            f"PostHog answered {path} with a web page instead of JSON (HTTP "
            f"{response.status_code}). host should be the PostHog app URL."
        ) from None


def _retry_delay(headers: Any, attempt: int) -> float:
    """Seconds to wait: the Retry-After header, else exponential backoff."""
    retry_after = (headers or {}).get("Retry-After")
    if retry_after is not None:
        try:
            return min(float(retry_after), _MAX_RETRY_DELAY_SECONDS)
        except ValueError:
            pass
    return min(2.0**attempt, _MAX_RETRY_DELAY_SECONDS)


# ---------------------------------------------------------------------------
# Sync
# ---------------------------------------------------------------------------
def sync_project(
    api: PostHogAPI,
    kinds: Sequence[str],
    state: dict,
    stats: dict | None = None,
    check_active: Callable[[], None] | None = None,
) -> Iterator[dict]:
    """Yield changed documents of the selected kinds, then tombstones for vanished ones."""
    if stats is None:
        stats = api.stats
    api.stats = stats  # requests are counted in the same dictionary
    stats.clear()
    stats.update(dict.fromkeys(_STAT_KEYS, 0))

    # Read everything before yielding anything: a failed page aborts the run here,
    # before a partial listing could turn into tombstones.
    objects: dict[str, list[dict]] = {}
    for kind in kinds:
        objects[kind] = []
        for item in api.list(kind):
            if not isinstance(item, dict) or item.get("id") is None:
                stats["failed"] += 1
                continue
            if not item.get("deleted"):
                objects[kind].append(item)

    context = RenderContext.from_objects(api.host, api.project_id, objects)
    rows = {}
    for kind in kinds:
        for item in objects[kind]:
            row = RENDERERS[kind](item, context)
            rows[row["id"]] = row

    known: dict = state.setdefault("documents", {})
    selection = sorted(kinds)
    if not rows and known and state.get("selection") == selection:
        raise PostHogSyncError(
            f"PostHog listed nothing for the same selection as the last run, which synced "
            f"{len(known)} documents. Refusing to forget them; check the API key and the "
            "project id. To forget them, change kinds=."
        )
    current = {doc_id: _digest(row) for doc_id, row in rows.items()}
    changed = sorted(doc_id for doc_id, digest in current.items() if known.get(doc_id) != digest)
    deleted = sorted(set(known) - set(current))

    outgoing = [rows[doc_id] for doc_id in changed]
    outgoing += [{"id": doc_id, "_deleted": True} for doc_id in deleted]
    for row in outgoing:
        if check_active is not None:
            check_active()
        yield row

    known.clear()
    known.update(current)
    state["selection"] = selection
    stats.update(
        scanned=len(current),
        skipped=len(current) - len(changed),
        changed=len(changed),
        deleted=len(deleted),
        complete=1,
    )
    logger.info(
        "PostHog: %d objects listed — %d changed, %d deletion(s).",
        len(current),
        len(changed),
        len(deleted),
    )


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------
class RenderContext:
    """Cross-references between objects, so documents can name what they link to."""

    def __init__(
        self,
        host: str,
        project_id: str,
        dashboard_names: Mapping[str, str] | None = None,
        dashboard_insights: Mapping[str, list[str]] | None = None,
        experiment_names: Mapping[str, str] | None = None,
    ):
        self.host = host.rstrip("/")
        self.project_id = str(project_id)
        self.dashboard_names = dict(dashboard_names or {})
        self.dashboard_insights = dict(dashboard_insights or {})
        self.experiment_names = dict(experiment_names or {})

    @classmethod
    def from_objects(cls, host: str, project_id: str, objects: Mapping[str, list[dict]]):
        dashboard_names = {
            str(d["id"]): _text(d.get("name")) or f"Dashboard {d['id']}"
            for d in objects.get("dashboards", [])
        }
        dashboard_insights: dict[str, list[str]] = {}
        for insight in objects.get("insights", []):
            for dashboard_id in insight.get("dashboards") or []:
                dashboard_insights.setdefault(str(dashboard_id), []).append(_insight_name(insight))
        experiment_names = {
            str(e["id"]): _text(e.get("name")) or f"Experiment {e['id']}"
            for e in objects.get("experiments", [])
        }
        return cls(host, project_id, dashboard_names, dashboard_insights, experiment_names)

    def url(self, kind: str, object_id: Any) -> str:
        segment = KINDS[kind][2]
        return f"{self.host}/project/{quote(self.project_id, safe='')}/{segment}/{object_id}"


def insight_to_row(insight: Mapping[str, Any], context: RenderContext) -> dict:
    name = _insight_name(insight)
    lines = [f"PostHog insight {name}."]
    _add(lines, _text(insight.get("description")))
    _add(lines, describe_query(insight.get("query")))
    _add_tags(lines, insight.get("tags"))
    dashboards = sorted(
        context.dashboard_names.get(str(d), f"dashboard {d}")
        for d in insight.get("dashboards") or []
    )
    if dashboards:
        lines.append("On dashboards: " + ", ".join(dashboards) + ".")
    lines.extend(describe_result(insight.get("query"), insight.get("result")))
    short_id = insight.get("short_id") or insight["id"]
    return _row(
        "insight", insight["id"], f"Insight: {name}", lines, context.url("insights", short_id)
    )


def dashboard_to_row(dashboard: Mapping[str, Any], context: RenderContext) -> dict:
    name = _text(dashboard.get("name")) or f"Dashboard {dashboard['id']}"
    lines = [f"PostHog dashboard {name}."]
    _add(lines, _text(dashboard.get("description")))
    if dashboard.get("pinned"):
        lines.append("Pinned.")
    _add_tags(lines, dashboard.get("tags"))
    insights = sorted(context.dashboard_insights.get(str(dashboard["id"]), []))
    if insights:
        lines.append("Insights on it: " + ", ".join(insights) + ".")
    url = context.url("dashboards", dashboard["id"])
    return _row("dashboard", dashboard["id"], f"Dashboard: {name}", lines, url)


def cohort_to_row(cohort: Mapping[str, Any], context: RenderContext) -> dict:
    name = _text(cohort.get("name")) or f"Cohort {cohort['id']}"
    lines = [f"PostHog cohort {name}."]
    _add(lines, _text(cohort.get("description")))
    lines.append(
        "Static cohort (a fixed list of people)." if cohort.get("is_static") else "Dynamic cohort."
    )
    if cohort.get("count") is not None:
        lines.append(f"Size: {cohort['count']} people.")
    criteria = describe_properties((cohort.get("filters") or {}).get("properties"))
    _add(lines, f"Criteria: {criteria}." if criteria else "")
    return _row(
        "cohort", cohort["id"], f"Cohort: {name}", lines, context.url("cohorts", cohort["id"])
    )


def feature_flag_to_row(flag: Mapping[str, Any], context: RenderContext) -> dict:
    key = _text(flag.get("key")) or f"flag {flag['id']}"
    lines = [f"PostHog feature flag {key}."]
    _add(lines, _text(flag.get("name")))  # PostHog stores a flag's description as its name
    status = "active" if flag.get("active") else "inactive"
    lines.append(f"Status: {status}" + (", archived." if flag.get("archived") else "."))
    filters = flag.get("filters") or {}
    for group in filters.get("groups") or []:
        who = describe_properties(group.get("properties")) or "everyone"
        rollout = group.get("rollout_percentage")
        share = f"{rollout}% of " if rollout is not None else ""
        variant = f" (variant {group['variant']})" if group.get("variant") else ""
        lines.append(f"Release condition: {share}{who}{variant}.")
    variants = (filters.get("multivariate") or {}).get("variants") or []
    if variants:
        parts = [f"{v.get('key')} {v.get('rollout_percentage', 0)}%" for v in variants]
        lines.append("Variants: " + ", ".join(parts) + ".")
    experiments = sorted(
        context.experiment_names.get(str(e), f"experiment {e}")
        for e in flag.get("experiment_set") or []
    )
    if experiments:
        lines.append("Used by experiments: " + ", ".join(experiments) + ".")
    _add_tags(lines, flag.get("tags"))
    url = context.url("feature_flags", flag["id"])
    return _row("feature_flag", flag["id"], f"Feature flag: {key}", lines, url)


def experiment_to_row(experiment: Mapping[str, Any], context: RenderContext) -> dict:
    name = _text(experiment.get("name")) or f"Experiment {experiment['id']}"
    lines = [f"PostHog experiment {name}."]
    _add(lines, _text(experiment.get("description")))
    if experiment.get("feature_flag_key"):
        lines.append(f"Feature flag: {experiment['feature_flag_key']}.")
    variants = (experiment.get("parameters") or {}).get("feature_flag_variants") or []
    if variants:
        parts = [f"{v.get('key')} {v.get('rollout_percentage', 0)}%" for v in variants]
        lines.append("Variants: " + ", ".join(parts) + ".")
    start, end = experiment.get("start_date"), experiment.get("end_date")
    if start and end:
        lines.append(f"Ran from {str(start)[:10]} to {str(end)[:10]}.")
    elif start:
        lines.append(f"Running since {str(start)[:10]}.")
    else:
        lines.append("Not started yet (draft).")
    if experiment.get("archived"):
        lines.append("Archived.")
    if experiment.get("conclusion"):
        lines.append(f"Conclusion: {str(experiment['conclusion']).replace('_', ' ')}.")
    _add(lines, _text(experiment.get("conclusion_comment")))
    _add_tags(lines, experiment.get("tags"))
    url = context.url("experiments", experiment["id"])
    return _row("experiment", experiment["id"], f"Experiment: {name}", lines, url)


RENDERERS: dict[str, Callable[[Mapping[str, Any], RenderContext], dict]] = {
    "insights": insight_to_row,
    "dashboards": dashboard_to_row,
    "cohorts": cohort_to_row,
    "feature_flags": feature_flag_to_row,
    "experiments": experiment_to_row,
}

_QUERY_KINDS = {
    "TrendsQuery": "Trends",
    "FunnelsQuery": "Funnel",
    "RetentionQuery": "Retention",
    "PathsQuery": "Paths",
    "StickinessQuery": "Stickiness",
    "LifecycleQuery": "Lifecycle",
}


def describe_query(query: Any) -> str:
    """The insight's question in words: kind, series, breakdown and date range."""
    if not isinstance(query, dict):
        return ""
    source = query.get("source") if isinstance(query.get("source"), dict) else query
    kind = str(source.get("kind") or "")
    if kind == "HogQLQuery":
        sql = _text(source.get("query"))[:_MAX_SQL_CHARS]
        return f"SQL query: {sql}" if sql else ""
    parts = [_QUERY_KINDS.get(kind, kind.removesuffix("Query") or "Query")]
    series = [_describe_series(node) for node in source.get("series") or []]
    series = [s for s in series if s]
    if series:
        joiner = " then " if kind == "FunnelsQuery" else ", "
        parts.append("of " + joiner.join(series))
    breakdown = (source.get("breakdownFilter") or {}).get("breakdown")
    if isinstance(breakdown, list):
        breakdown = ", ".join(str(b) for b in breakdown)
    if breakdown:
        parts.append(f"broken down by {breakdown}")
    date_from = (source.get("dateRange") or {}).get("date_from")
    if date_from:
        parts.append(f"from {date_from}")
    return "Query: " + " ".join(parts) + "."


def _describe_series(node: Any) -> str:
    if not isinstance(node, dict):
        return ""
    label = node.get("custom_name") or node.get("event") or node.get("name")
    if label is None and node.get("kind") == "ActionsNode":
        label = f"action {node.get('id')}"
    if not label:
        label = "all events"
    math = node.get("math")
    return f"{label} ({str(math).replace('_', ' ')})" if math and math != "total" else str(label)


def describe_result(query: Any, result: Any) -> list[str]:
    """A short, deterministic summary of an insight's cached result (trends and funnels)."""
    if not isinstance(result, list) or not result:
        return []
    source = (
        query.get("source")
        if isinstance(query, dict) and isinstance(query.get("source"), dict)
        else query
    )
    kind = (source or {}).get("kind") if isinstance(source, dict) else None
    lines: list[str] = []
    if kind == "FunnelsQuery" and all(isinstance(step, dict) for step in result):
        steps = sorted(result, key=lambda step: step.get("order", 0))
        first = steps[0].get("count") or 0
        for step in steps[:_MAX_RESULT_LINES]:
            label = step.get("custom_name") or step.get("name")
            lines.append(f"Step {int(step.get('order', 0)) + 1}, {label}: {step.get('count')}.")
        last = steps[-1].get("count") or 0
        if first:
            lines.append(f"Overall conversion: {round(100 * last / first, 1)}%.")
    elif kind == "TrendsQuery":
        for series in result[:_MAX_RESULT_LINES]:
            if not isinstance(series, dict):
                continue
            label = series.get("label") or (series.get("action") or {}).get("name")
            breakdown = series.get("breakdown_value")
            if breakdown not in (None, "", []):
                label = f"{label} ({breakdown})"
            value = series.get("aggregated_value")
            if value is None:
                value = series.get("count")
            if value is not None:
                lines.append(f"{label}: {_number(value)}.")
    if lines:
        lines.insert(0, "Latest cached result:")
    return lines


def describe_properties(properties: Any) -> str:
    """Render PostHog property filters (nested AND/OR groups) as words."""
    if isinstance(properties, list):
        parts = [describe_properties(p) for p in properties]
        return " and ".join(p for p in parts if p)
    if not isinstance(properties, dict):
        return ""
    if "values" in properties:
        parts = [describe_properties(v) for v in properties.get("values") or []]
        parts = [p for p in parts if p]
        joiner = " or " if str(properties.get("type", "AND")).upper() == "OR" else " and "
        text = joiner.join(parts)
        return f"({text})" if len(parts) > 1 else text
    key = properties.get("key")
    if key is None:
        return ""
    if properties.get("type") == "cohort":
        return f"in cohort {properties.get('value')}"
    operator = str(properties.get("operator") or "exact").replace("_", " ")
    value = properties.get("value")
    if isinstance(value, list):
        value = ", ".join(str(v) for v in value)
    if operator in ("is set", "is not set"):
        return f"{key} {operator}"
    return f"{key} {operator} {value}"


def _insight_name(insight: Mapping[str, Any]) -> str:
    return (
        _text(insight.get("name"))
        or _text(insight.get("derived_name"))
        or f"Insight {insight.get('short_id') or insight.get('id')}"
    )


def _row(kind: str, object_id: Any, title: str, lines: list[str], url: str) -> dict:
    return {
        "id": f"{kind}:{object_id}",
        "title": _clean(title),
        "content": "\n".join(_clean(line) for line in lines),
        "url": url,
        "_deleted": False,
    }


def _add(lines: list[str], text: str) -> None:
    if text:
        lines.append(text)


def _add_tags(lines: list[str], tags: Any) -> None:
    names = [str(tag) for tag in tags or [] if tag]
    if names:
        lines.append("Tags: " + ", ".join(names) + ".")


def _text(value: Any) -> str:
    return " ".join(str(value).split()) if value else ""


def _clean(text: str) -> str:
    return _UNSAFE_CHARS.sub("", text)


def _number(value: Any) -> str:
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    if isinstance(value, float):
        return f"{value:.2f}"
    return str(value)


def _digest(row: Mapping[str, Any]) -> str:
    return hashlib.sha256(json.dumps(row, sort_keys=True).encode()).hexdigest()
