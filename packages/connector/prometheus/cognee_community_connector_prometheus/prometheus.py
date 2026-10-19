"""dlt source for Prometheus alerting and recording rules.

Built on dlt's declarative ``rest_api`` source. It makes one call, two with the
opt-in metric metadata, and has no hand-written HTTP:

* ``GET /api/v1/rules``, one document per alerting or recording rule
* ``GET /api/v1/metadata`` (``include_metadata=True``), one document per metric
  with its type, unit and help text

No samples are read and no PromQL is run. Rules are the part of a Prometheus
server that people write by hand: what each alert checks, how long it waits,
who it pages and where its runbook is.

Why it is built this way:

Document path. The source sets ``cognee_document_source``, so every rule is its
own text document with a content-hash id. One edited rule re-processes one
document. On the relational path any change re-emits the whole source.

Definitions only. The rules response mixes each rule's definition with its
runtime state: ``state``, ``health``, ``lastError``, ``evaluationTime``,
``lastEvaluation`` and the active ``alerts``. Those change between two reads of
an unchanged server, so a row copied from the whole object would get a new
content hash on every sync. Each row is rebuilt from named definition fields,
and ``exclude_alerts=true`` keeps the active alerts off the wire.

Rules have no id. A rule name can repeat inside a group (one rule per severity
is common), so the row id is a hash of file, group, type, name and labels. Two
rules that match on all of those get a counter, in the order the server lists
them.

Full snapshot. Prometheus has no change feed for rules, but the whole rule list
is one request, so each run reads all of it under ``write_disposition="replace"``.
Unchanged rules keep their content hash and are not re-cognified. A rule that
was removed from the server drops out of the snapshot and cognee's
``orphan_cleanup`` forgets it.

Fail closed. Staging is authoritative under replace, so a short read would be
forgotten as if it were a deletion. An HTTP error, a body that is not JSON, a
``status`` other than ``success`` or a missing ``data.groups`` list raises and
aborts the run, which leaves memory untouched. An empty group list is accepted
on a first sync, but refused after an earlier sync had rules: a server whose
rule files did not load, or a filter that matches nothing, looks exactly like
that. ``allow_empty=True`` lets that sync through.

One set of tables per server. The table names start with a resource name taken
from the server's address, so two servers synced into one dataset keep their
own rows instead of replacing each other's.
"""

import hashlib
import json
import os
import re
from collections import Counter
from collections.abc import Callable
from typing import Any
from urllib.parse import urlsplit

from cognee.tasks.ingestion.dlt_utils import DOCUMENT_SOURCE_ATTR, PIPELINE_SCOPE_ATTR

PROMETHEUS_SOURCE_NAME = "prometheus"
# Staging-table names for resource_name="prometheus". cognee scopes orphan
# cleanup by table, so they are part of the contract: renaming one forgets and
# re-ingests its rows. By default the resource name comes from the server's
# address, which gives prometheus_<host>_<port>_rules and so on.
PROMETHEUS_RULES_TABLE = "prometheus_rules"
PROMETHEUS_METRICS_TABLE = "prometheus_metrics"

# The raw API pages. They are piped into the row builders and never loaded.
_RULE_GROUPS = "prometheus_rule_groups"
_METADATA = "prometheus_metadata"

# The values the rules API's `type` parameter accepts.
_RULE_TYPES = ("alert", "record")
# What a resource name may look like: it becomes part of the table names.
_RESOURCE_NAME = re.compile(r"[a-z][a-z0-9_]*")
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

_EXTRA_HINT = "The Prometheus connector requires dlt: pip install 'dlt[sqlalchemy]>=1.9.0,<2'."


def prometheus_source(
    base_url: str | None = None,
    username: str | None = None,
    password: str | None = None,
    token: str | None = None,
    headers: dict[str, str] | None = None,
    rule_type: str | None = None,
    rule_groups: list[str] | None = None,
    include_metadata: bool = False,
    allow_empty: bool = False,
    resource_name: str | None = None,
    check_active: Callable[[], None] | None = None,
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
        allow_empty: Let a sync through that finds no rules (or no metric
            metadata) although an earlier sync found some. Off by default,
            because a server whose rule files did not load, or a filter that
            matches nothing, looks like that, and it would forget everything.
            Turn it on once to forget rules that really were all removed.
        resource_name: Prefix of the staging tables and the key of this source's
            dlt state. Defaults to one taken from the server's address, such as
            ``prometheus_localhost_9090``, so two servers synced into one dataset
            keep separate tables. Pass one to keep the same tables when the
            address changes. Lowercase letters, digits and underscores.
        check_active: Called before every response is used. A host raises from
            it to stop a sync whose access was revoked mid-run; the run then
            aborts and memory stays as it was.
        session: ``requests.Session`` to send requests through (mainly a
            test-injection point). When omitted dlt's own session is used, which
            retries 429 and 5xx responses.

    Returns:
        A dlt source suitable for ``cognee.add(...)`` / ``cognee.remember(...)``.
    """
    try:
        import dlt
        from dlt.sources.rest_api import rest_api_resources
    except ImportError as exc:
        raise ImportError(_EXTRA_HINT) from exc

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
            f"resource_name must be lowercase letters, digits and underscores, not {name!r}."
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

    resources: list[dict] = [
        {
            "name": _RULE_GROUPS,
            "endpoint": {
                "path": "api/v1/rules",
                "params": params,
                "data_selector": "data.groups",
                # The whole rule list comes back in one response.
                "paginator": "single_page",
                "response_actions": [*active, _require_success("groups", allow_empty)],
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
                    "response_actions": [*active, _require_success(None)],
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
    if session is not None:
        client["session"] = session

    by_name = {
        resource.name: resource
        for resource in rest_api_resources({"client": client, "resources": resources})
    }

    @dlt.transformer(name=rules_table, primary_key="id", write_disposition="replace")
    def prometheus_rules(groups):
        # A fresh counter per call: the rule list is one page, so this is called
        # once per run, and a source object that is run twice keeps its ids.
        # The type and group filters are applied here as well, for a server that
        # ignores the `type` or `rule_group[]` parameter (older Prometheus does).
        yield from _rule_rows(groups, Counter(), rule_type, rule_groups)

    loaded = [by_name[_RULE_GROUPS] | prometheus_rules]

    if include_metadata:

        @dlt.transformer(name=metrics_table, primary_key="id", write_disposition="replace")
        def prometheus_metrics(bodies):
            # Prometheus builds metadata from live scrape targets, so right after a
            # restart it answers with an empty object for a while. Refuse that only
            # when an earlier load had metrics, or every metric would be forgotten.
            # A server that never had targets just loads no metrics. The flag sits
            # in source state: dlt clears a resource's own state on every replace.
            state = dlt.current.source_state()
            for body in bodies if isinstance(bodies, list) else [bodies]:
                data = body.get("data") if isinstance(body, dict) else None
                if not data and state.get("had_metrics") and not allow_empty:
                    raise ValueError(
                        "Prometheus: metric metadata came back empty after earlier loads "
                        "had metrics, as it does right after a restart. Aborting so no "
                        "metric is forgotten; run again once the first scrapes are done."
                    )
                state["had_metrics"] = bool(data)
                yield from _metric_rows(data or {})

        loaded.append(by_name[_METADATA] | prometheus_metrics)

    # max_table_nesting=0: rows are flat already, and this keeps dlt from ever
    # unpacking a nested value into a child table that cognee would read back.
    @dlt.source(name=name, max_table_nesting=0)
    def _prometheus():
        return loaded

    source = _prometheus()
    # Opt into the document ingestion path (row -> text document -> cognify).
    # Set on the object that is returned: resolve_dlt_sources reads it from there.
    setattr(source, DOCUMENT_SOURCE_ATTR, PROMETHEUS_SOURCE_NAME)
    # Keep this source's dlt state (the empty-snapshot flags) per dataset. Without
    # a scope, every unscoped dlt source shares one pipeline and its state.
    setattr(source, PIPELINE_SCOPE_ATTR, name)
    return source


# ---------------------------------------------------------------------------
# Response guard and row builders (module-private)
# ---------------------------------------------------------------------------


def _default_resource_name(parts) -> str:
    """Name a server's tables after its host, port and path prefix."""
    where = f"{parts.hostname or ''}_{parts.port or ''}_{parts.path}".lower()
    slug = re.sub(r"[^a-z0-9]+", "_", where).strip("_")
    name = f"{PROMETHEUS_SOURCE_NAME}_{slug}" if slug else PROMETHEUS_SOURCE_NAME
    if len(name) > _MAX_RESOURCE_NAME:
        digest = hashlib.sha256(name.encode()).hexdigest()[:8]
        name = f"{name[: _MAX_RESOURCE_NAME - 9]}_{digest}"
    return name


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


def _require_success(key: str | None, allow_empty: bool = False):
    """Build a response hook that aborts unless Prometheus answered ``success``.

    ``key`` names the list expected under ``data`` (``"groups"`` for rules).
    ``None`` expects ``data`` itself to be an object, as metadata returns it. With
    ``data_selector`` set, a body of another shape would yield no rows and no
    error, and under replace an empty read forgets everything. A body that carries
    ``warnings`` is refused too: Thanos Querier answers ``success`` with only the
    reachable rulers' groups and a warning when one is down. For rules, an empty
    group list is refused when an earlier sync had rules, unless ``allow_empty``.
    The flag is kept in the source's dlt state, which is only saved when a load
    succeeds.
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
        if key == "groups":
            import dlt

            state = dlt.current.source_state()
            if not data[key] and state.get("had_rules") and not allow_empty:
                raise ValueError(
                    f"Prometheus: no rule groups from {_where(response)}, although an "
                    "earlier sync found some. Rule files that did not load, or a filter "
                    "that matches nothing, look like this. Aborting so memory is not "
                    "wiped; pass allow_empty=True if the rules really were all removed."
                )
            state["had_rules"] = bool(data[key])

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
        "title": f"Prometheus {kind} rule {name} in group {group.get('name')}",
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
                "id": metric,
                "title": f"Prometheus metric {metric}",
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
    """Render ``label: value`` lines, skipping empty values."""
    return [f"{label}: {value}" for label, value in pairs if value not in (None, "")]
