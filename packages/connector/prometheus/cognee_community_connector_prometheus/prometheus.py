"""dlt source for Prometheus alerting and recording rules.

Calls, both through dlt's declarative ``rest_api`` source:

* ``GET /api/v1/rules``: one document per alerting or recording rule
* ``GET /api/v1/metadata``: one document per metric, only with ``include_metadata=True``

Rules the code keeps:

* Rows hold definition fields only. ``state``, ``health``, ``lastEvaluation`` and
  the active alerts change between two reads of an unchanged server and would
  give every row a new content hash on every sync.
* Rules have no id, so the row id is a hash of file, group, type, name and
  labels, with a counter for rules that match on all of those.
* Each run is a full snapshot under ``replace``, and cognee forgets what left it.
* A failed, malformed, partial or suspiciously empty read aborts the run, so a
  bad answer never wipes memory.
"""

import hashlib
import json
import os
import re
from collections import Counter
from collections.abc import Callable
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlsplit

import dlt
from cognee.shared.logging_utils import get_logger
from cognee.tasks.ingestion import dlt_utils
from dlt.sources.helpers.requests import Client
from dlt.sources.rest_api import rest_api_resources

logger = get_logger("prometheus_connector")

# The provider tag cognee puts on every document from this source. Orphan
# cleanup matches on it, so it stays the same for every server.
PROMETHEUS_SOURCE_NAME = "prometheus"
# Staging-table names for resource_name="prometheus". cognee scopes orphan
# cleanup by table, so a renamed table is never cleaned: its documents stay
# next to the new table's copies (the README says how to clear them). By
# default the resource name comes from the server's address, which gives
# prometheus_<host>_<port>_rules and so on.
PROMETHEUS_RULES_TABLE = "prometheus_rules"
PROMETHEUS_METRICS_TABLE = "prometheus_metrics"

# The raw API pages. They are piped into the row builders and never loaded.
_RULE_GROUPS = "prometheus_rule_groups"
_METADATA = "prometheus_metadata"

# The values the rules API's `type` parameter accepts.
_RULE_TYPES = ("alert", "record")
# Lowercase words joined by single underscores: the name becomes part of the
# table names, and dlt folds "a__b" and a trailing "_" into other names.
_RESOURCE_NAME = re.compile(r"[a-z][a-z0-9]*(?:_[a-z0-9]+)*")
# Longer derived names are cut and given a hash, so the table names stay under
# the 63 characters Postgres allows for an identifier.
_MAX_RESOURCE_NAME = 48
# Annotations most rules carry, in the order a reader wants them. Any other
# annotation follows in key order.
_KNOWN_ANNOTATIONS = (
    ("summary", "Summary"),
    ("description", "Description"),
    ("runbook_url", "Runbook"),
)
# Seconds before one request gives up. dlt's default is 60, and the wait happens
# inside cognee's staging lock, which every other dlt ingestion waits on.
_REQUEST_TIMEOUT = 30
# Longest wait before the one retry a 5xx or a dropped connection gets. dlt's
# own cap is 300 s, four times per request; a few seconds rides out a blip
# without holding the staging lock for long. A 429 is never retried.
_RETRY_WAIT = 5
# Control characters other than tab and newline. A NUL would make Postgres
# refuse the whole load, so every sync would fail while that rule exists.
_CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")


def prometheus_source(
    *,
    base_url: str | None = None,
    username: str | None = None,
    password: str | None = None,
    token: str | None = None,
    headers: dict[str, str] | None = None,
    rule_type: str | None = None,
    rule_groups: list[str] | None = None,
    include_metadata: bool = False,
    allow_empty: bool = False,
    allow_empty_metrics: bool = False,
    resource_name: str | None = None,
    check_active: Callable[[], None] | None = None,
    service: Any = None,
    session: Any = None,
):
    """Create a dlt source that yields Prometheus rules, and optionally metric metadata.

    Args:
        base_url: Prometheus server URL, including any path prefix set with
            ``--web.route-prefix``. Falls back to ``PROMETHEUS_URL``.
        username: Basic-auth user, for a server behind a proxy or started with a
            web config file. Falls back to ``PROMETHEUS_USERNAME``.
        password: Basic-auth password. Falls back to ``PROMETHEUS_PASSWORD``.
        token: Bearer token, for a server behind a proxy that expects one. Falls
            back to ``PROMETHEUS_TOKEN``. Use either a token or basic auth.
        headers: Extra headers sent with every request.
        rule_type: ``"alert"`` or ``"record"`` to ingest only one kind of rule.
            ``None`` ingests both.
        rule_groups: Ingest only these rule groups. ``None`` ingests every group.
        include_metadata: Also ingest type, unit and help text for every metric
            the server knows. Off by default: a server can know thousands.
        allow_empty: Let one sync through that finds no rules although an earlier
            sync found some, or that lacks groups of ``rule_groups``. Off by
            default, because a server whose rule files did not load, or a filter
            that matches nothing, looks like that, and it would forget
            everything. Missing groups are remembered, so later runs skip them
            with a warning and list them in ``cognee_sync_stats``, until they
            are back.
        allow_empty_metrics: Let one sync through whose metric metadata has
            under half the metrics of the last sync. Off by default: right
            after a restart Prometheus reports metadata only for the targets
            scraped so far. Separate from ``allow_empty``, so forgetting rules
            on purpose never forgets metrics by accident.
        resource_name: Prefix of the staging tables and the key of this source's
            dlt state. Defaults to one taken from the server's address, such as
            ``prometheus_localhost_9090_<hash>``, so two servers synced into one dataset
            keep separate tables. Pass one to keep the same tables when the
            address changes. Lowercase words joined by single underscores.
        check_active: Called before every response is used. A host raises from
            it to stop a sync whose access was revoked mid-run; the run then
            aborts and memory stays as it was.
        service: ``requests.Session`` to send requests through, mainly for tests.
            By default dlt's session is used, set so a sync never waits long
            inside cognee's staging lock: a 429 stops the run, a 5xx or a
            dropped connection gets one retry after at most 5 s, and each
            request times out after 30 s. ``session`` is accepted as another
            name for it.

    Returns:
        A dlt source for ``cognee.add(...)`` / ``cognee.remember(...)``. Its
        ``cognee_sync_stats`` holds the row counts of the last run.
    """
    # cognee marks its document-sync behaviour with this number. Version 1, in
    # 1.6.1, is the one that scopes orphan cleanup by staging table, which two
    # tables under one tag need. Later versions only add to it.
    if getattr(dlt_utils, "DOCUMENT_SYNC_VERSION", 0) < 1:
        raise RuntimeError("The Prometheus connector needs cognee>=1.6.1 (table-scoped cleanup).")
    url = base_url or os.environ.get("PROMETHEUS_URL")
    if not url:
        raise ValueError("Prometheus URL required: pass base_url= or set PROMETHEUS_URL.")
    parts = urlsplit(url)
    if parts.query or parts.fragment or url.endswith(("?", "#")):
        # dlt appends "api/v1/rules" to the base URL as text, after any query.
        raise ValueError("Prometheus URL must not contain a query string or a fragment.")
    if parts.username or parts.password:
        # They would show up in dlt's logs and in error messages.
        raise ValueError("Pass credentials as username/password or token, not in the URL.")
    if parts.path.rstrip("/").endswith("/api/v1"):
        raise ValueError("Pass the server URL without /api/v1: the connector adds the API path.")
    name = resource_name or _default_resource_name(parts)
    if not _RESOURCE_NAME.fullmatch(name):
        raise ValueError(
            f"resource_name must be lowercase words joined by single underscores, not {name!r}."
        )
    rules_table, metrics_table = f"{name}_rules", f"{name}_metrics"
    active = [_still_active(check_active)] if check_active else []
    if token is None and username is None and password is None:
        # Only when no credentials were passed, so an explicit choice is never
        # mixed with the other kind from the environment.
        username = os.environ.get("PROMETHEUS_USERNAME")
        password = os.environ.get("PROMETHEUS_PASSWORD")
        token = os.environ.get("PROMETHEUS_TOKEN")
    if token and (username or password):
        raise ValueError("Pass either a bearer token or basic-auth credentials, not both.")
    if bool(username) != bool(password):
        raise ValueError("Basic auth needs both a username and a password.")
    if rule_type is not None and rule_type not in _RULE_TYPES:
        raise ValueError(f"rule_type must be one of {_RULE_TYPES} or None, not {rule_type!r}.")
    if isinstance(rule_groups, str):
        raise ValueError("rule_groups must be a list of group names, not a single string.")

    params: dict[str, Any] = {"exclude_alerts": "true"}
    if rule_type:
        params["type"] = rule_type
    if rule_groups:
        # dlt runs query params through str.format, so braces in a group name
        # would turn into a different filter (and an empty snapshot). Escape them.
        params["rule_group[]"] = [
            group.replace("{", "{{").replace("}", "}}") for group in rule_groups
        ]

    stats: dict[str, int] = {}
    resources: list[dict] = [
        {
            "name": _RULE_GROUPS,
            "endpoint": {
                "path": "api/v1/rules",
                "params": params,
                # All of `data`, so the row builder runs even when there are no
                # groups, and the empty check can see the rules left after the
                # filters below.
                "data_selector": "data",
                # The whole rule list comes back in one response.
                "paginator": "single_page",
                "response_actions": [*active, _rate_limited(stats), _require_success("groups")],
            },
        }
    ]
    if include_metadata:
        resources.append(
            {
                "name": _METADATA,
                "endpoint": {
                    "path": "api/v1/metadata",
                    # The whole body, so the row builder runs even when `data` is
                    # empty and can tell a restart from a server without targets.
                    "data_selector": "$",
                    "paginator": "single_page",
                    "response_actions": [*active, _rate_limited(stats), _require_success(None)],
                },
            }
        )

    # dlt joins "api/v1/rules" onto the base URL, so a path prefix is kept.
    client: dict[str, Any] = {"base_url": url}
    if token:
        client["auth"] = {"type": "bearer", "token": token}
    elif username:
        client["auth"] = {"type": "http_basic", "username": username, "password": password}
    if headers:
        client["headers"] = dict(headers)
    http = service or session or _default_session()
    client["session"] = http

    by_name = {
        resource.name: resource
        for resource in rest_api_resources({"client": client, "resources": resources})
    }

    # The rules table is a plain resource that reads the raw rules page itself,
    # listed first, so its first step runs before any request of the run.
    rules = dlt.resource(
        _rule_documents, name=rules_table, primary_key="id", write_disposition="replace"
    )
    loaded = [rules(by_name[_RULE_GROUPS], rule_type, rule_groups, allow_empty, stats, rules_table)]

    if include_metadata:

        @dlt.transformer(name=metrics_table, primary_key="id", write_disposition="replace")
        def prometheus_metrics(bodies):
            # Prometheus builds metadata from live scrape targets, so right after a
            # restart it answers with nothing, then fills in target by target over
            # a scrape interval. A sync in that window would forget the metrics of
            # every target not scraped yet. A server that never had targets just
            # loads no metrics.
            for body in bodies if isinstance(bodies, list) else [bodies]:
                data = body.get("data") if isinstance(body, dict) else None
                rows = list(_metric_rows(data or {}))
                _guard_metric_drop(len(rows), allow_empty_metrics)
                yield rows
                _finish(stats, "metrics", metrics_table, len(rows))

        loaded.append(by_name[_METADATA] | prometheus_metrics)

    # max_table_nesting=0: rows are flat already, and this keeps dlt from ever
    # unpacking a nested value into a child table that cognee would read back.
    @dlt.source(name=name, max_table_nesting=0)
    def _prometheus():
        return loaded

    source = _prometheus()
    # Opt into the document ingestion path (row -> text document -> cognify).
    # Set on the object that is returned: resolve_dlt_sources reads it from there.
    setattr(source, dlt_utils.DOCUMENT_SOURCE_ATTR, PROMETHEUS_SOURCE_NAME)
    # Keep this source's dlt state (the empty-snapshot flags) per dataset. Without
    # a scope, every unscoped dlt source shares one pipeline and its state.
    setattr(source, dlt_utils.PIPELINE_SCOPE_ATTR, name)
    source.cognee_sync_stats = stats
    return source


# ---------------------------------------------------------------------------
# Response guard and row builders (module-private)
# ---------------------------------------------------------------------------


def _default_resource_name(parts) -> str:
    """Name a server's tables after its host, port and path prefix.

    The readable part folds every other character into "_", so
    ``prom-eu.example.com`` and ``prom.eu.example.com`` would share tables and
    keep replacing each other's rows. A short hash of the exact address keeps
    them apart.
    """
    exact = f"{parts.hostname or ''}:{parts.port or ''}{parts.path.rstrip('/')}"
    digest = hashlib.sha256(exact.encode()).hexdigest()[:8]
    where = f"{parts.hostname or ''}_{parts.port or ''}_{parts.path}".lower()
    slug = re.sub(r"[^a-z0-9]+", "_", where).strip("_")
    room = _MAX_RESOURCE_NAME - len(PROMETHEUS_SOURCE_NAME) - len(digest) - 2
    slug = slug[:room].rstrip("_")
    return "_".join(part for part in (PROMETHEUS_SOURCE_NAME, slug, digest) if part)


def _default_session():
    """dlt's session, kept short because it runs inside cognee's staging lock.

    A 5xx or a dropped connection gets one retry after at most ``_RETRY_WAIT``
    seconds, so one blip doesn't fail the run. A 429 is never retried: it is
    left out of the retried codes, and ``_rate_limited`` stops the run from
    inside the request before dlt's retry would see the answer anyway. Each try
    gives up after ``_REQUEST_TIMEOUT``.
    """
    return Client(
        raise_for_status=False,
        status_codes=tuple(range(500, 600)),  # dlt's default list also has 429
        request_max_attempts=2,
        request_max_retry_delay=_RETRY_WAIT,
        request_timeout=_REQUEST_TIMEOUT,
    ).session


def _rate_limited(stats: dict):
    """Build a response hook that stops the run on a 429 instead of waiting.

    A proxy or Thanos in front of Prometheus can rate-limit. Waiting it out
    would hold cognee's staging lock, which every other dlt ingestion in the
    process waits on, and the next run re-reads everything anyway.
    """

    def check(response, *args, **kwargs):
        if response.status_code == 429:
            stats["failed_rate_limit"] = stats.get("failed_rate_limit", 0) + 1
            raise ValueError(
                f"Prometheus: rate limited (429) on {_where(response)}. Stopping instead "
                "of waiting inside cognee's staging lock; memory is unchanged, run again later."
            )

    return check


def _rule_documents(
    raw,
    rule_type: str | None,
    rule_groups: list[str] | None,
    allow_empty: bool,
    stats: dict,
    table: str,
):
    """One document per rule, from the single rules page of this run."""
    # Before the first request of the run: clear the counts, so a failed run
    # never shows the last good run's numbers as its own.
    stats.clear()
    stats["failed_rate_limit"] = 0
    stats["skipped_groups"] = []
    pages = list(raw)
    data = pages[0] if len(pages) == 1 else None
    groups = data.get("groups") if isinstance(data, dict) else None
    # A fresh counter per run, so a source object that is run twice keeps its
    # ids. The type and group filters are applied here as well, for a server
    # that ignores `type` or `rule_group[]` (older Prometheus does).
    rows = list(_rule_rows(groups, Counter(), rule_type, rule_groups))
    _guard_missing_groups(groups, rule_type, rule_groups, allow_empty, stats)
    _guard_empty("rules", len(rows), allow_empty)
    yield rows
    _finish(stats, "rules", table, len(rows))


def _guard_missing_groups(
    groups: Any,
    rule_type: str | None,
    rule_groups: list[str] | None,
    allow_empty: bool,
    stats: dict,
) -> None:
    """Refuse a sync that lacks a rule group the caller asked for by name.

    A group that was renamed, or whose file did not load, is otherwise forgotten
    without a word while the other groups load. With ``rule_type`` set the
    server may leave out a group that has no rule of that type, so the check is
    skipped then. ``allow_empty=True`` lets one run forget the missing groups.
    Each is then kept in dlt state with the time of that run, so later runs
    skip it instead of stopping, until it is back. Skipped groups are listed in
    ``cognee_sync_stats["skipped_groups"]`` and counted in a warning on every
    run. Log messages give counts only: group names are the caller's own labels.
    """
    if not rule_groups or rule_type or not isinstance(groups, list):
        return
    state = dlt.current.source_state()
    present = {group.get("name") for group in groups if isinstance(group, dict)}
    missing = sorted(set(rule_groups) - present)
    accepted = state.get("forgotten_groups") or {}
    if isinstance(accepted, list):  # written by an earlier commit, time unknown
        accepted = dict.fromkeys(accepted, "an earlier run")
    new = [group for group in missing if group not in accepted]
    if new and not allow_empty:
        raise ValueError(
            f"Prometheus: {len(new)} of the {len(set(rule_groups))} rule groups in "
            "rule_groups are not on the server. Aborting rather than forgetting their "
            "rules. Pass allow_empty=True for one run to forget them; later runs then "
            "skip them with a warning."
        )
    now = _now()
    kept = {group: accepted.get(group, now) for group in missing}
    for when in sorted(set(kept.values())):
        logger.warning(
            "Prometheus: skipping %s rule groups of rule_groups: not on the server, "
            "accepted as gone by the allow_empty=True run of %s",
            sum(1 for group in missing if kept[group] == when),
            when,
        )
    stats["skipped_groups"] = missing
    state["forgotten_groups"] = kept


def _now() -> str:
    """The time a run accepted a missing group, as shown in its warnings."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def _guard_metric_drop(count: int, allow_empty_metrics: bool) -> None:
    """Refuse metric metadata with under half the metrics of the last load.

    The count from the last successful load lives in the source's dlt state,
    which is only saved when a load succeeds.
    """
    before = dlt.current.source_state().get("rows", {}).get("metrics", 0)
    if before and count < before / 2 and not allow_empty_metrics:
        raise ValueError(
            f"Prometheus: metric metadata has {count} metrics, under half of the "
            f"{before} the last sync loaded, as it does right after a restart while "
            "targets are still being scraped. Aborting so no metric is forgotten; run "
            "again later, or pass allow_empty_metrics=True if they really went away."
        )


def _guard_empty(kind: str, count: int, allow_empty: bool) -> None:
    """Refuse an empty snapshot when the last successful one had rows.

    Rule files that did not load, or a filter that matches nothing, answer with
    a valid empty list, and replace would forget everything. The flag lives in
    the source's dlt state, which is only saved when a load succeeds.
    """
    state = dlt.current.source_state()
    if count == 0 and state.get(f"had_{kind}") and not allow_empty:
        raise ValueError(
            f"Prometheus: no {kind} at all, although an earlier sync found some. Rule "
            "files that did not load, or a filter that matches nothing, look like this. "
            "Aborting so memory is not wiped; pass allow_empty=True if they really "
            "were all removed."
        )
    state[f"had_{kind}"] = count > 0


def _finish(stats: dict[str, int], kind: str, table: str, count: int) -> None:
    """Record and log how many rows a table got, and warn when it emptied."""
    state = dlt.current.source_state().setdefault("rows", {})
    if count == 0 and state.get(kind):
        logger.warning(
            "Prometheus: %s is empty this run after %s rows last run; cognee will forget them",
            table,
            state[kind],
        )
    state[kind] = count
    stats[kind] = count
    logger.info("Prometheus: %s rows in %s", count, table)


def _where(response) -> str:
    """The endpoint a response came from, without its query string.

    Error messages keep to the status and the path: the query carries the rule
    group names a user filtered on.
    """
    return urlsplit(response.url).path


def _still_active(check_active: Callable[[], None]):
    """Build a response hook that runs the host's ``check_active`` before a page is used."""

    def check(response, *args, **kwargs):
        check_active()

    return check


def _require_success(key: str | None):
    """Build a response hook that aborts unless Prometheus answered ``success``.

    ``key`` names the list expected under ``data`` (``"groups"`` for rules).
    ``None`` expects ``data`` itself to be an object, as metadata returns it. A
    body of another shape would yield no rows and no error, and under replace an
    empty read forgets everything. A body that carries ``warnings`` is refused
    too: Thanos Querier answers ``success`` with only the reachable rulers'
    groups and a warning when one is down.
    """

    def check(response, *args, **kwargs):
        if not response.ok:
            return  # dlt raises for error statuses once the hooks have run
        try:
            body = response.json()
        except ValueError as exc:
            raise ValueError(
                f"Prometheus: the response from {_where(response)} is not JSON. "
                "Aborting so a partial snapshot cannot forget live rows."
            ) from exc
        data = body.get("data") if isinstance(body, dict) else None
        if key is None:
            shaped = isinstance(data, dict)
        else:
            shaped = isinstance(data, dict) and isinstance(data.get(key), list)
        if not isinstance(body, dict) or body.get("status") != "success" or not shaped:
            expected = "an object under 'data'" if key is None else f"a 'data.{key}' list"
            raise ValueError(
                f"Prometheus: expected status 'success' and {expected} from "
                f"{_where(response)}. Aborting so a partial snapshot cannot forget live rows."
            )
        if body.get("warnings"):
            raise ValueError(
                f"Prometheus: the response from {_where(response)} carries warnings, so it may "
                "be partial. Aborting so a partial snapshot cannot forget live rows."
            )

    return check


def _rule_rows(
    groups: Any,
    seen: Counter,
    rule_type: str | None = None,
    rule_groups: list[str] | None = None,
):
    """Yield one row per rule, with the group's name, file and interval copied in.

    ``rule_type`` (``"alert"`` or ``"record"``) and ``rule_groups`` drop what a
    server returned although it was not asked for.
    """
    kind = {"alert": "alerting", "record": "recording"}.get(rule_type or "")
    for group in groups if isinstance(groups, list) else [groups]:
        rules = group.get("rules") if isinstance(group, dict) else None
        if not isinstance(rules, list):
            raise ValueError("Prometheus: a rule group without a 'rules' list. Aborting.")
        if rule_groups and group.get("name") not in rule_groups:
            continue
        for rule in rules:
            if kind and rule.get("type") != kind:
                continue
            yield _rule_row(group, rule, seen)


def _rule_row(group: dict, rule: dict, seen: Counter) -> dict:
    """Build a rule document row from definition fields only."""
    kind = rule.get("type")  # "alerting" or "recording"
    name = rule.get("name")
    labels = rule.get("labels") or {}
    annotations = rule.get("annotations") or {}

    identity = json.dumps(
        [group.get("file"), group.get("name"), kind, name, sorted(labels.items())]
    )
    seen[identity] += 1
    if seen[identity] > 1:
        identity = f"{identity}#{seen[identity]}"

    known = {key for key, _ in _KNOWN_ANNOTATIONS}
    lines = _lines(
        ("Type", f"{kind} rule" if kind else None),
        ("Name", name),
        ("Group", group.get("name")),
        ("File", group.get("file")),
        ("Evaluation interval", _duration(group.get("interval"))),
        ("Alert limit", group.get("limit") or None),
        ("Expression", rule.get("query")),
        ("For", _duration(rule.get("duration"))),
        ("Keep firing for", _duration(rule.get("keepFiringFor"))),
        ("Labels", ", ".join(f"{key}={value}" for key, value in sorted(labels.items()))),
        *((label, annotations.get(key)) for key, label in _KNOWN_ANNOTATIONS),
        *((f"Annotation {key}", annotations[key]) for key in sorted(set(annotations) - known)),
    )
    return {
        "id": hashlib.sha256(identity.encode()).hexdigest()[:32],
        "title": _clean(f"Prometheus {kind} rule {name} in group {group.get('name')}"),
        "content": "\n".join(lines),
    }


def _metric_rows(pages: Any):
    """Yield one row per metric from the metadata object (metric name -> entries)."""
    for page in pages if isinstance(pages, list) else [pages]:
        if not isinstance(page, dict):
            raise ValueError("Prometheus: metric metadata is not an object. Aborting.")
        for metric in sorted(page):
            entries = page[metric] if isinstance(page[metric], list) else []
            # Targets can report different help text for one metric: keep each
            # distinct entry once, in a fixed order.
            distinct = sorted(
                {
                    (entry.get("type") or "", entry.get("unit") or "", entry.get("help") or "")
                    for entry in entries
                    if isinstance(entry, dict)
                }
            )
            lines = [
                line
                for kind, unit, text in distinct
                for line in _lines(("Type", kind), ("Unit", unit), ("Help", text))
            ]
            yield {
                "id": _clean(metric),
                "title": _clean(f"Prometheus metric {metric}"),
                "content": "\n".join(lines),
            }


def _duration(seconds: Any) -> str | None:
    """Render seconds the way rule files write them: 600 -> "10m", 1.5 -> "1s500ms"."""
    if isinstance(seconds, bool) or not isinstance(seconds, (int, float)) or seconds <= 0:
        return None
    # Whole milliseconds, Prometheus's own precision for durations.
    remaining, parts = round(seconds * 1000), []
    for unit, size in (("d", 86_400_000), ("h", 3_600_000), ("m", 60_000), ("s", 1000), ("ms", 1)):
        count, remaining = divmod(remaining, size)
        if count:
            parts.append(f"{count}{unit}")
    return "".join(parts) or None


def _lines(*pairs: tuple[str, Any]) -> list[str]:
    """Render ``label: value`` lines as clean text, skipping empty values.

    Control characters are dropped and text that is not valid UTF-8 is replaced,
    so a stray byte in a rule file cannot fail the load.
    """
    return [_clean(f"{label}: {value}") for label, value in pairs if value not in (None, "")]


def _clean(text: str) -> str:
    """Drop control characters and replace text that is not valid UTF-8."""
    return _CONTROL.sub("", text).encode("utf-8", "replace").decode("utf-8")
