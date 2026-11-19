"""Argo CD connector for cognee — a ``dlt`` source that turns Argo CD applications into memory.

Read applications from an Argo CD server with an API token and hand the resource
straight to :func:`cognee.remember`::

    import cognee
    from cognee_community_connector_argocd import argocd_source

    await cognee.remember(
        argocd_source("https://argocd.example.com", instance_name="prod"),
        dataset_name="argocd",
        primary_key="id",
        write_disposition="merge",   # required by the connector's resource
        max_rows_per_table=0,        # 0 = no row cap (the default cap is 50)
    )

Design
------
* **Auth** — an Argo CD API token, sent as ``Authorization: Bearer <token>``. A
  read-only account (``apiKey`` capability, ``applications, get`` permission) is enough.
* **Documents** — one text document per Application (state, health and its message,
  conditions, last operation, unhealthy resources, lineage) and one per sync-history
  entry. Volatile fields (``reconciledAt``, ``resourceVersion`` ...) are never rendered,
  so the same application always renders to the same text.
* **Secure by default** — TLS is verified, Helm values and parameters are left out unless
  ``include_helm_values=True``, annotations and manifests are never read, and credentials
  are stripped from repository URLs.
* **Errors never pass silently** — any failed request raises with a hint, and the run
  changes nothing.
* **Incremental sync** — one list request per run. Each application's ``resourceVersion`` is
  kept in the dlt resource state, but only as a shortcut: Argo CD bumps it on every
  reconciliation (``reconciledAt``), so the hash of the rendered content decides whether a
  row is emitted. History entries never change, so each is emitted once.
* **Forget-on-delete** — an application missing from the list (or being deleted) is emitted
  as ``{"id": ..., "_deleted": True}`` together with its history documents. Tombstones are
  yielded before the upserts, and the state is saved after the last row.
* **Guards** — Argo CD's list endpoint silently leaves out applications the token may not
  read, so an empty list while applications are known, or a loss of more than half of them
  (at least 5), raises and forgets nothing unless ``allow_mass_delete=True``.

Not implemented: the watch stream, periodic full reconciliation, a ``userinfo`` pre-check and
commit metadata.
"""

import hashlib
import json
import os
import random
import re
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from cognee.shared.logging_utils import get_logger
from cognee.tasks.ingestion import dlt_utils
from cognee.tasks.ingestion.dlt_utils import DOCUMENT_SOURCE_ATTR

logger = get_logger("argocd_connector")

ID_PREFIX = "argocd:"
LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})
INSTANCE_NAME_PATTERN = re.compile(r"^[A-Za-z0-9._-]+$")
READONLY_POLICY = "p, role:app-reader, applications, get, */*, allow"

RETRY_STATUSES = frozenset({429, 502, 503, 504})
MAX_RETRIES = 5
BACKOFF_BASE = 1.0
BACKOFF_CAP = 30.0
RETRY_AFTER_CAP = 60.0

# Bump whenever the rendered text of a document changes, so every application is re-rendered.
RENDER_VERSION = 1
# Forgetting at least this many applications, and more than half of the known ones, needs
# allow_mass_delete=True.
MASS_DELETE_MINIMUM = 5

MESSAGE_LIMIT = 500
MAX_LISTED = 20  # resources needing attention, managed resources and labels per document

# Replaced in tests so retries do not wait.
_sleep = time.sleep
_warned: set[str] = set()


@dataclass(frozen=True)
class _ArgoConfig:
    base_url: str
    instance: str
    projects: tuple[str, ...]
    app_names: tuple[str, ...]
    app_namespace: str | None
    selector: str | None
    include_resources: bool
    include_history: bool
    history_limit: int | None
    include_helm_values: bool
    verify: bool | str
    timeout: float
    allow_mass_delete: bool = False


def _warn_once(message: str) -> None:
    if message not in _warned:
        _warned.add(message)
        logger.warning(message)


# ---------------------------------------------------------------------------
# HTTP client
# ---------------------------------------------------------------------------
def _make_session(api_token: str) -> Any:
    """A ``requests`` session that sends the API token as a bearer token."""
    import requests

    session = requests.Session()
    session.headers.update({"Authorization": f"Bearer {api_token}", "Accept": "application/json"})
    return session


def _server_message(response: Any) -> str:
    """The ``message`` field of Argo CD's JSON error body, or an empty string."""
    try:
        body = response.json()
    except ValueError:
        return ""
    message = body.get("message") if isinstance(body, dict) else None
    return str(message) if message else ""


def _retry_delay(attempt: int, retry_after: str | None) -> float:
    if retry_after:
        try:
            return min(max(float(retry_after), 0.0), RETRY_AFTER_CAP)
        except ValueError:
            pass  # an HTTP-date: fall back to the exponential delay
    return min(BACKOFF_BASE * 2**attempt, BACKOFF_CAP) + random.uniform(0, 1)


def _connection_error(exc: Exception) -> RuntimeError:
    import requests

    if isinstance(exc, requests.exceptions.SSLError):
        hint = (
            "TLS verification failed. Argo CD uses a self-signed certificate by default: "
            "pass ca_bundle=<path to its CA certificate>, or verify_ssl=False for a throwaway "
            "local test."
        )
    elif isinstance(exc, requests.exceptions.Timeout):
        hint = "The request timed out. Raise `timeout`, or filter with `projects`/`selector`."
    else:
        hint = (
            "Is the port-forward still running "
            "(kubectl port-forward svc/argocd-server -n argocd 8080:443)? "
            "Check the host and port in base_url, and its path if Argo CD runs with --rootpath."
        )
    return RuntimeError(f"Could not reach Argo CD ({type(exc).__name__}). {hint}")


def _status_error(response: Any, url_path: str) -> RuntimeError:
    status = response.status_code
    server = _server_message(response)
    detail = f" Server said: {server}" if server else ""
    if status == 401:
        return RuntimeError(
            "Argo CD rejected the API token (HTTP 401): it is invalid or expired. Create a new "
            f"one with `argocd account generate-token --account <name>`.{detail}"
        )
    if status == 403:
        return RuntimeError(
            "Argo CD denied access (HTTP 403). The account needs read access to applications, "
            f"for example this argocd-rbac-cm policy: `{READONLY_POLICY}` plus "
            f"`g, <account>, role:app-reader`.{detail}"
        )
    if status == 404:
        return RuntimeError(
            f"Argo CD returned HTTP 404 for {url_path}. Check base_url: it must include the "
            f"--rootpath prefix if the server runs under one.{detail}"
        )
    return RuntimeError(f"Argo CD returned HTTP {status} for {url_path}.{detail}")


def _get_json(
    session: Any,
    url: str,
    params: list[tuple[str, str]] | None = None,
    *,
    verify: bool | str = True,
    timeout: float = 60.0,
) -> Any:
    """GET ``url`` and return its JSON body, retrying 429/502/503/504 with backoff."""
    import requests

    path = urlsplit(url).path
    for attempt in range(MAX_RETRIES + 1):
        try:
            response = session.get(url, params=params or [], verify=verify, timeout=timeout)
        except requests.exceptions.RequestException as exc:
            raise _connection_error(exc) from exc
        if response.status_code in RETRY_STATUSES and attempt < MAX_RETRIES:
            delay = _retry_delay(attempt, response.headers.get("Retry-After"))
            logger.warning(
                "Argo CD returned HTTP %s; retrying in %.1fs (%s/%s).",
                response.status_code,
                delay,
                attempt + 1,
                MAX_RETRIES,
            )
            _sleep(delay)
            continue
        if not 200 <= response.status_code < 300:
            raise _status_error(response, path)
        try:
            return response.json()
        except ValueError as exc:
            raise RuntimeError(
                f"Argo CD answered {path} with something that is not JSON. Check base_url "
                "(and its --rootpath prefix); a proxy login page can cause this."
            ) from exc
    raise AssertionError("unreachable")  # pragma: no cover


def _list_applications(session: Any, config: _ArgoConfig) -> list[dict]:
    """Every application the token can read, filtered by the configured scope."""
    params = [("projects", project) for project in config.projects]
    params += [("names", name) for name in config.app_names]
    if config.selector:
        params.append(("selector", config.selector))
    if config.app_namespace:
        params.append(("appNamespace", config.app_namespace))
    body = _get_json(
        session,
        f"{config.base_url}/api/v1/applications",
        params,
        verify=config.verify,
        timeout=config.timeout,
    )
    if not isinstance(body, dict):
        raise RuntimeError("Argo CD returned an unexpected body for the application list.")
    # Argo CD sends `items: null` when nothing matches.
    return [app for app in body.get("items") or [] if isinstance(app, dict)]


# ---------------------------------------------------------------------------
# Rendering helpers
# ---------------------------------------------------------------------------
def _strip_credentials(url: str) -> str:
    """Drop user:password@, query and fragment from a URL; leave other text alone."""
    if "://" not in url:
        return url
    parts = urlsplit(url)
    host = parts.netloc.rpartition("@")[2]
    return urlunsplit((parts.scheme, host, parts.path, "", ""))


def _clip(value: Any, limit: int = MESSAGE_LIMIT) -> str:
    """One-line text, cut to ``limit`` characters."""
    text = " ".join(str(value or "").split())
    return text if len(text) <= limit else text[: limit - 3] + "..."


def _namespace_of(app: dict) -> str:
    namespace = (app.get("metadata") or {}).get("namespace")
    if namespace:
        return namespace
    _warn_once("Argo CD application without a namespace; using 'default' in its document id.")
    return "default"


def _sources_of(spec: dict) -> list[dict]:
    sources = spec.get("sources") or ([spec["source"]] if spec.get("source") else [])
    return [source for source in sources if isinstance(source, dict)]


def _source_summary(source: dict) -> str:
    text = _strip_credentials(source.get("repoURL") or "") or "unknown repository"
    if source.get("chart"):
        text += f", chart {source['chart']}"
    elif source.get("path"):
        text += f", path {source['path']}"
    if source.get("targetRevision"):
        text += f", target revision {source['targetRevision']}"
    if source.get("ref"):
        text += f", ref {source['ref']}"
    return text


def _source_lines(sources: list[dict]) -> list[str]:
    if not sources:
        return []
    if len(sources) == 1:
        return [f"Source: {_source_summary(sources[0])}"]
    return ["Sources:"] + [f"- {_source_summary(s)}" for s in sources]


def _destination_text(destination: dict) -> str:
    cluster = destination.get("name") or _strip_credentials(destination.get("server") or "")
    text = f"cluster {cluster or 'unknown'}"
    if destination.get("namespace"):
        text += f", namespace {destination['namespace']}"
    return text


def _sync_policy_text(spec: dict) -> str:
    policy = spec.get("syncPolicy") or {}
    automated = policy.get("automated")
    if automated is None:
        text = "manual"
    else:
        flags = automated if isinstance(automated, dict) else {}
        enabled = [
            label
            for label, key in (("prune", "prune"), ("self-heal", "selfHeal"))
            if flags.get(key)
        ]
        text = "automated" + (f" ({', '.join(enabled)})" if enabled else "")
    options = sorted(str(option) for option in policy.get("syncOptions") or [])
    return text + (f"; options: {', '.join(options)}" if options else "")


def _revisions_text(sync: dict) -> str:
    revisions = [str(r) for r in sync.get("revisions") or []]
    return sync.get("revision") or ", ".join(revisions)


def _sync_line(status: dict) -> str:
    sync = status.get("sync") or {}
    text = f"Sync status: {sync.get('status') or 'Unknown'}"
    revision = _revisions_text(sync)
    return text + (f" (compared to revision {revision})" if revision else "")


def _health_line(status: dict) -> str:
    health = status.get("health") or {}
    text = f"Health: {health.get('status') or 'Unknown'}"
    return text + (f" - {_clip(health['message'])}" if health.get("message") else "")


def _conditions_lines(status: dict) -> list[str]:
    conditions = sorted(
        (c.get("type") or "Unknown", _clip(c.get("message")))
        for c in status.get("conditions") or []
        if isinstance(c, dict)
    )
    if not conditions:
        return []
    return ["Conditions:"] + [
        f"- {kind}: {message}" if message else f"- {kind}" for kind, message in conditions
    ]


def _operation_line(status: dict) -> str | None:
    state = status.get("operationState")
    if not isinstance(state, dict) or not state:
        return None
    kind = "Sync" if (state.get("operation") or {}).get("sync") is not None else "Operation"
    parts = [kind, state.get("phase") or "Unknown"]
    revision = (state.get("syncResult") or {}).get("revision")
    if revision:
        parts.append(f"revision {revision}")
    if state.get("startedAt"):
        parts.append(f"started {state['startedAt']}")
    if state.get("finishedAt"):
        parts.append(f"finished {state['finishedAt']}")
    if state.get("message"):
        parts.append(f"message: {_clip(state['message'])}")
    return "Last operation: " + ", ".join(parts)


def _resource_name(resource: dict) -> str:
    kind = resource.get("kind") or "Resource"
    name = resource.get("name") or "unknown"
    namespace = resource.get("namespace")
    return f"{kind} {namespace}/{name}" if namespace else f"{kind} {name}"


def _resource_sort_key(resource: dict) -> tuple[str, str, str]:
    return (
        resource.get("kind") or "",
        resource.get("namespace") or "",
        resource.get("name") or "",
    )


def _needs_attention(resource: dict) -> bool:
    health = (resource.get("health") or {}).get("status")
    return resource.get("status") == "OutOfSync" or bool(health and health != "Healthy")


def _attention_line(resource: dict) -> str:
    health = resource.get("health") or {}
    states = [resource.get("status") or "", health.get("status") or ""]
    text = ", ".join(state for state in states if state) or "unknown state"
    return f"- {_resource_name(resource)}: {text}" + (
        f" - {_clip(health['message'])}" if health.get("message") else ""
    )


def _resource_lines(status: dict) -> list[str]:
    resources = sorted(
        (r for r in status.get("resources") or [] if isinstance(r, dict)),
        key=_resource_sort_key,
    )
    if not resources:
        return []
    kinds: dict[str, int] = {}
    for resource in resources:
        kind = resource.get("kind") or "Resource"
        kinds[kind] = kinds.get(kind, 0) + 1
    by_kind = ", ".join(f"{kind} {count}" for kind, count in sorted(kinds.items()))
    lines = [f"Resources: {len(resources)} total ({by_kind})"]
    attention = [r for r in resources if _needs_attention(r)]
    if attention:
        lines.append("Resources needing attention:")
        lines += [_attention_line(r) for r in attention[:MAX_LISTED]]
        if len(attention) > MAX_LISTED:
            lines.append(f"- and {len(attention) - MAX_LISTED} more")
    return lines


def _helm_lines(sources: list[dict]) -> list[str]:
    lines: list[str] = []
    for index, source in enumerate(sources, start=1):
        helm = source.get("helm") or {}
        parameters = sorted(
            f"{p.get('name')}={p.get('value')}"
            for p in helm.get("parameters") or []
            if isinstance(p, dict)
        )
        value_files = sorted(str(f) for f in helm.get("valueFiles") or [])
        values = helm.get("valuesObject")
        values_text = json.dumps(values, sort_keys=True) if values else (helm.get("values") or "")
        if not (parameters or value_files or values_text):
            continue
        lines.append(f"Helm (source {index}):")
        if parameters:
            lines.append("- parameters: " + "; ".join(parameters))
        if value_files:
            lines.append("- value files: " + ", ".join(value_files))
        if values_text:
            lines.append("- values: " + str(values_text).strip())
    return lines


def _lineage_lines(
    name: str, project: str, sources: list[dict], destination: dict, managed: list[dict]
) -> list[str]:
    lines = [f"Application {name} belongs to project {project}."]
    for source in sources:
        lines.append(f"Application {name} deploys {_source_summary(source)}.")
    lines.append(f"Application {name} deploys to {_destination_text(destination)}.")
    if managed:
        names = [_resource_name(r) for r in managed[:MAX_LISTED]]
        more = f", and {len(managed) - MAX_LISTED} more" if len(managed) > MAX_LISTED else ""
        lines.append(f"Application {name} manages {', '.join(names)}{more}.")
    return lines


def _label_line(metadata: dict) -> str | None:
    labels = sorted(f"{k}={v}" for k, v in (metadata.get("labels") or {}).items())
    if not labels:
        return None
    more = f", and {len(labels) - MAX_LISTED} more" if len(labels) > MAX_LISTED else ""
    return "Labels: " + ", ".join(labels[:MAX_LISTED]) + more


def _application_content(config: _ArgoConfig, app: dict, namespace: str, name: str) -> str:
    metadata, spec, status = (app.get(k) or {} for k in ("metadata", "spec", "status"))
    project = spec.get("project") or "default"
    sources = _sources_of(spec)
    destination = spec.get("destination") or {}
    images = sorted({str(i) for i in (status.get("summary") or {}).get("images") or []})

    lines = [
        f"Argo CD application: {name}",
        f"Instance: {config.instance}",
        f"Namespace: {namespace}",
        f"Project: {project}",
        *_source_lines(sources),
        f"Destination: {_destination_text(destination)}",
        f"Sync policy: {_sync_policy_text(spec)}",
        _sync_line(status),
        _health_line(status),
        *_conditions_lines(status),
    ]
    operation = _operation_line(status)
    if operation:
        lines.append(operation)
    resource_lines = _resource_lines(status) if config.include_resources else []
    lines += resource_lines
    if images:
        lines.append("Images: " + ", ".join(images))
    labels = _label_line(metadata)
    if labels:
        lines.append(labels)
    if config.include_helm_values:
        lines += _helm_lines(sources)
    managed = []
    if config.include_resources:
        managed = sorted(
            (r for r in status.get("resources") or [] if isinstance(r, dict)),
            key=_resource_sort_key,
        )
    lines += ["", *_lineage_lines(name, project, sources, destination, managed)]
    return "\n".join(lines)


def _application_row(config: _ArgoConfig, app: dict) -> dict | None:
    metadata = app.get("metadata") or {}
    name = metadata.get("name")
    if not name:
        return None
    namespace = _namespace_of(app)
    return {
        "id": _application_id(config.instance, namespace, name),
        "title": f"Argo CD application {name}",
        "content": _application_content(config, app, namespace, name),
        "_deleted": False,
    }


def _application_id(instance: str, namespace: str, name: str) -> str:
    return f"{ID_PREFIX}{instance}:{namespace}/{name}"


def _history_id(instance: str, namespace: str, name: str, entry_id: int) -> str:
    return f"{_application_id(instance, namespace, name)}:history:{entry_id}"


def _initiator_text(initiated_by: dict) -> str:
    if initiated_by.get("automated"):
        return "automated sync"
    if initiated_by.get("username"):
        return f"user {initiated_by['username']}"
    return "unknown"


def _history_entries(config: _ArgoConfig, app: dict) -> list[dict]:
    """History entries that have an id, newest first, cut to ``history_limit``."""
    entries = [
        e
        for e in (app.get("status") or {}).get("history") or []
        if isinstance(e, dict) and isinstance(e.get("id"), int)
    ]
    entries.sort(key=lambda e: e["id"], reverse=True)
    return entries[: config.history_limit] if config.history_limit else entries


def _history_row(config: _ArgoConfig, namespace: str, name: str, entry: dict) -> dict:
    """One sync. It mentions only facts that never change, so it can be written once."""
    entry_id = entry["id"]
    revisions = [str(r) for r in entry.get("revisions") or []]
    revision = entry.get("revision") or ", ".join(revisions)
    lines = [
        f"Argo CD sync {entry_id} of application {name}",
        f"Instance: {config.instance}",
        f"Namespace: {namespace}",
        f"Revision: {revision or 'unknown'}",
    ]
    if entry.get("deployStartedAt"):
        lines.append(f"Deploy started: {entry['deployStartedAt']}")
    if entry.get("deployedAt"):
        lines.append(f"Deployed: {entry['deployedAt']}")
    lines.append(f"Initiated by: {_initiator_text(entry.get('initiatedBy') or {})}")
    lines += _source_lines(
        _sources_of({"source": entry.get("source"), "sources": entry.get("sources")})
    )
    lines += [
        "",
        f"Sync {entry_id} deployed revision {revision or 'unknown'} for application {name} "
        f"on Argo CD instance {config.instance}.",
    ]
    return {
        "id": _history_id(config.instance, namespace, name, entry_id),
        "title": f"Argo CD sync {entry_id} of {name}",
        "content": "\n".join(lines),
        "_deleted": False,
    }


# ---------------------------------------------------------------------------
# Sync (pure given a session + state dict — unit-testable)
# ---------------------------------------------------------------------------
def _digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _config_fingerprint(config: _ArgoConfig) -> str:
    """Changes whenever the scope or the rendering of the documents changes."""
    payload = {
        "projects": sorted(config.projects),
        "app_names": sorted(config.app_names),
        "app_namespace": config.app_namespace,
        "selector": config.selector,
        "include_resources": config.include_resources,
        "include_history": config.include_history,
        "history_limit": config.history_limit,
        "include_helm_values": config.include_helm_values,
        "render_version": RENDER_VERSION,
    }
    return _digest(json.dumps(payload, sort_keys=True))


def _index_applications(apps: list[dict], config: _ArgoConfig, stats: dict) -> dict[str, dict]:
    """Listed applications by document id. A nameless one is skipped; a repeat id keeps the last."""
    listed: dict[str, dict] = {}
    for app in apps:
        name = (app.get("metadata") or {}).get("name")
        if not name:
            stats["skipped"] += 1
            continue
        app_id = _application_id(config.instance, _namespace_of(app), name)
        if app_id in listed:
            logger.warning("Argo CD listed the same application twice; using the last one.")
        listed[app_id] = app
    return listed


def _stored_ids(app_id: str, entry: dict) -> list[str]:
    """Every document of a stored application: the application and its history entries."""
    return [app_id, *entry.get("history", [])]


def _refusal(reason: str) -> RuntimeError:
    return RuntimeError(
        f"{reason} Argo CD silently leaves out applications the token may not read, so this can "
        "be a missing permission (check the token's RBAC policy), wrong projects/app_names/"
        "selector filters, or an unhealthy server. Nothing was forgotten and the saved state is "
        "unchanged. Set allow_mass_delete=True only if those applications were really deleted."
    )


def _plan_application(
    config: _ArgoConfig, app: dict, old: dict | None, rescan: bool, stats: dict
) -> tuple[list[dict], list[str], dict]:
    """Decide what one live application needs.

    Returns the rows to emit, the history ids to forget, and the application's new state.
    ``resourceVersion`` only short-cuts the work: Argo CD bumps it on every reconciliation
    (``reconciledAt``), so the rendered content's hash decides whether a row is emitted.
    """
    old = old or {}
    resource_version = str((app.get("metadata") or {}).get("resourceVersion") or "")
    if not rescan and resource_version and old.get("rv") == resource_version:
        stats["unchanged"] += 1
        return [], [], old

    row = _application_row(config, app)
    digest = _digest(row["content"])
    rows: list[dict] = []
    if old.get("hash") == digest:
        stats["unchanged"] += 1
    else:
        rows.append(row)
        stats["applications"] += 1

    history = list(old.get("history", []))
    forgotten: list[str] = []
    if config.include_history:
        known = set(history)
        name, namespace = app["metadata"]["name"], _namespace_of(app)
        # Oldest first, so the stored list stays in sync order.
        for entry in reversed(_history_entries(config, app)):
            document = _history_row(config, namespace, name, entry)
            if document["id"] not in known:
                rows.append(document)
                history.append(document["id"])
                stats["history"] += 1
    else:
        forgotten, history = history, []
    return rows, forgotten, {"rv": resource_version, "hash": digest, "history": history}


def _iter_rows(
    session: Any, config: _ArgoConfig, state: dict, stats: dict | None = None
) -> Iterator[dict]:
    """Yield tombstones for what vanished, then rows for what is new or changed.

    ``state`` is a plain dict standing in for dlt's resource state. The one list request,
    all rendering and all guards happen before the first yield, and the state is only written
    after the last one, so a failed run changes nothing.
    """
    if stats is None:
        stats = {}
    stats.clear()
    stats.update(applications=0, history=0, deleted=0, unchanged=0, skipped=0)

    stored = (state.get("instances") or {}).get(config.instance) or {}
    old_apps: dict[str, dict] = dict(stored.get("apps") or {})
    fingerprint = _config_fingerprint(config)
    rescan = stored.get("config") != fingerprint

    listed = _index_applications(_list_applications(session, config), config, stats)
    if not listed:
        if old_apps and not config.allow_mass_delete:
            raise _refusal("Argo CD returned no applications, but earlier runs saw some.")
        if not old_apps:
            logger.warning(
                "Argo CD returned no applications. The filters may match nothing, or the token "
                "may not be allowed to read any (Argo CD omits applications silently)."
            )

    rows: list[dict] = []
    forgotten: list[str] = []
    new_apps: dict[str, dict] = {}
    gone = 0
    for app_id in sorted(listed):
        app, old = listed[app_id], old_apps.get(app_id)
        if (app.get("metadata") or {}).get("deletionTimestamp"):
            # Argo CD is deleting it (a cascade delete keeps the object for a while).
            if old is None:
                stats["skipped"] += 1
            else:
                forgotten += _stored_ids(app_id, old)
                gone += 1
            continue
        app_rows, app_forgotten, new_apps[app_id] = _plan_application(
            config, app, old, rescan, stats
        )
        rows += app_rows
        forgotten += app_forgotten
    for app_id in sorted(set(old_apps) - set(listed)):
        forgotten += _stored_ids(app_id, old_apps[app_id])
        gone += 1

    if not config.allow_mass_delete and gone >= MASS_DELETE_MINIMUM and gone > len(old_apps) / 2:
        raise _refusal(f"{gone} of the {len(old_apps)} known applications would be forgotten.")
    stats["deleted"] = gone

    for document_id in sorted(set(forgotten)):
        yield {"id": document_id, "_deleted": True}
    yield from rows
    state.setdefault("instances", {})[config.instance] = {"config": fingerprint, "apps": new_apps}
    logger.info("Argo CD: sync stats %s", stats)


# ---------------------------------------------------------------------------
# Public factory
# ---------------------------------------------------------------------------
def _normalize_base_url(raw: str) -> tuple[str, str]:
    """Validate ``raw``; return (base URL without trailing slash, host)."""
    try:
        parts = urlsplit(raw.strip())
        host = parts.hostname
        parts.port  # noqa: B018 - raises ValueError for an invalid port
    except ValueError as exc:
        raise ValueError("base_url is not a valid URL.") from exc
    if parts.scheme not in ("http", "https") or not host:
        raise ValueError("base_url must look like https://argocd.example.com[/rootpath].")
    if "@" in parts.netloc:
        raise ValueError("base_url must not contain credentials; pass api_token instead.")
    if parts.query or parts.fragment:
        raise ValueError("base_url must not contain a query or fragment.")
    if parts.scheme == "http":
        if host not in LOOPBACK_HOSTS:
            raise ValueError("base_url must use https:// (http:// is only allowed for localhost).")
        _warn_once(
            "Argo CD base_url uses plain http:// on localhost; only do this for a local test."
        )
    return urlunsplit((parts.scheme, parts.netloc, parts.path.rstrip("/"), "", "")), host


def _resolve_instance(instance_name: str | None, host: str) -> str:
    instance = (instance_name or os.getenv("ARGOCD_INSTANCE_NAME") or "").strip()
    if not instance:
        if host in LOOPBACK_HOSTS:
            raise ValueError(
                "instance_name is required for a localhost base_url: every port-forward looks "
                "the same, and the name keeps document ids stable. Pass instance_name or set "
                "ARGOCD_INSTANCE_NAME."
            )
        instance = host
    if not INSTANCE_NAME_PATTERN.match(instance):
        raise ValueError(
            "instance_name may only contain letters, digits, '.', '_' and '-'. "
            "Pass instance_name explicitly."
        )
    return instance


def _resolve_list(value: list[str] | str | None, env_name: str, label: str) -> tuple[str, ...]:
    """Explicit argument first, then the environment. An explicit empty list is an error."""
    if value is None:
        value = [item for item in os.getenv(env_name, "").split(",") if item.strip()] or None
    if value is None:
        return ()
    if isinstance(value, str):
        value = [value]
    cleaned = tuple(dict.fromkeys(item.strip() for item in value if item and item.strip()))
    if not cleaned:
        raise ValueError(f"{label} is empty. Leave it out to read every application.")
    return cleaned


def _resolve_verify(verify_ssl: bool, ca_bundle: str | None) -> bool | str:
    if ca_bundle:
        if not verify_ssl:
            raise ValueError("ca_bundle and verify_ssl=False contradict each other.")
        path = os.path.expanduser(ca_bundle)
        if not os.path.isfile(path):
            raise ValueError("ca_bundle does not point to an existing file.")
        return path
    if not verify_ssl:
        _warn_once(
            "Argo CD TLS verification is turned off; do not use verify_ssl=False in production."
        )
    return verify_ssl


def argocd_source(
    base_url: str | None = None,
    *,
    api_token: str | None = None,
    instance_name: str | None = None,
    projects: list[str] | None = None,
    app_names: list[str] | None = None,
    app_namespace: str | None = None,
    selector: str | None = None,
    include_resources: bool = True,
    include_history: bool = True,
    history_limit: int | None = None,
    include_helm_values: bool = False,
    verify_ssl: bool = True,
    ca_bundle: str | None = None,
    timeout: float = 60.0,
    allow_mass_delete: bool = False,
    resource_name: str = "argocd_applications",
    check_active: Callable[[], None] | None = None,
    session: Any = None,
):
    """Return a ``dlt`` resource yielding one row per Argo CD application and sync.

    Hand the result to ``cognee.remember(...)`` with ``write_disposition="merge"``,
    ``primary_key="id"`` and ``max_rows_per_table=0``.

    Args:
        base_url: Argo CD server URL, with its ``--rootpath`` prefix if it has one. Falls back
            to ``ARGOCD_BASE_URL``. ``http://`` is accepted only for localhost.
        api_token: Argo CD API token (``ARGOCD_AUTH_TOKEN``). Never logged or put in a URL.
        instance_name: Short name of this Argo CD, used in document ids (``ARGOCD_INSTANCE_NAME``).
            Required for localhost URLs; otherwise defaults to the host name.
        projects: Only these projects (``ARGOCD_PROJECTS``, comma-separated).
        app_names: Only these applications (``ARGOCD_APPS``, comma-separated). An entry may be
            ``namespace/name``.
        app_namespace: Only applications in this namespace. Default: every enabled namespace.
        selector: Kubernetes label selector, like ``team=payments``.
        include_resources: Add the resource roll-up and the resources that need attention.
        include_history: Add one document per sync-history entry.
        history_limit: Keep only the newest N history entries per application.
        include_helm_values: Add Helm parameters and values. Off by default: they can hold
            secrets.
        verify_ssl: Verify the server certificate. Leave on outside a throwaway local test.
        ca_bundle: Path to a CA certificate file for a self-signed Argo CD.
        timeout: Seconds to wait for each request.
        allow_mass_delete: Let a sync forget every known application, or more than half of them
            (at least 5). Off by default: an empty or shrunken list is more often a missing
            permission or a wrong filter than a real mass deletion.
        resource_name: Stable dlt resource name. Give each Argo CD its own name when several
            sync into one dataset, so their state stays separate.
        check_active: Optional host authorization checkpoint during extraction.
        session: A pre-built ``requests``-style session, mainly for tests. It must send its own
            authentication.
    """
    try:
        import dlt
        import requests  # noqa: F401 - checked here so a missing install fails early
    except ImportError as exc:
        raise ImportError(
            "The Argo CD connector needs dlt and requests: "
            "pip install cognee-community-connector-argocd"
        ) from exc

    if getattr(dlt_utils, "DOCUMENT_SYNC_VERSION", 0) < 1:
        raise RuntimeError(
            "Argo CD sync requires a Cognee build with table-scoped DLT document "
            "cleanup. Upgrade Cognee before syncing to avoid deleting another source's data."
        )

    raw_url = base_url or os.getenv("ARGOCD_BASE_URL")
    if not raw_url:
        raise ValueError("base_url is required. Pass it or set ARGOCD_BASE_URL.")
    normalized_url, host = _normalize_base_url(raw_url)
    token = api_token or os.getenv("ARGOCD_AUTH_TOKEN")
    if not token:
        raise ValueError(
            "An Argo CD API token is required. Pass api_token or set ARGOCD_AUTH_TOKEN "
            "(create one with `argocd account generate-token --account <name>`)."
        )
    if history_limit is not None and history_limit < 1:
        raise ValueError("history_limit must be at least 1.")
    if timeout <= 0:
        raise ValueError("timeout must be greater than 0.")
    if not isinstance(allow_mass_delete, bool):
        raise ValueError("allow_mass_delete must be True or False.")

    config = _ArgoConfig(
        base_url=normalized_url,
        instance=_resolve_instance(instance_name, host),
        projects=_resolve_list(projects, "ARGOCD_PROJECTS", "projects"),
        app_names=_resolve_list(app_names, "ARGOCD_APPS", "app_names"),
        app_namespace=app_namespace,
        selector=selector,
        include_resources=include_resources,
        include_history=include_history,
        history_limit=history_limit,
        include_helm_values=include_helm_values,
        verify=_resolve_verify(verify_ssl, ca_bundle),
        timeout=timeout,
        allow_mass_delete=allow_mass_delete,
    )
    stats: dict[str, int] = {}

    @dlt.resource(
        name=resource_name,
        write_disposition="merge",
        primary_key="id",
        # `_deleted` is a boolean hard-delete marker: rows where it is True are removed
        # from the dlt destination on merge, which cognee's orphan cleanup then forgets.
        columns={"_deleted": {"data_type": "bool", "hard_delete": True}},
    )
    def argocd_applications():
        client = session if session is not None else _make_session(token)
        try:
            yield from dlt_utils.guarded_rows(
                _iter_rows(client, config, dlt.current.resource_state(), stats),
                check_active,
            )
        finally:
            if session is None:
                client.close()

    resource = argocd_applications()
    # Opt into the document ingestion path: each application and sync becomes a text
    # document that flows through normal cognify (LLM graph extraction).
    setattr(resource, DOCUMENT_SOURCE_ATTR, "argocd")
    setattr(resource, dlt_utils.PIPELINE_SCOPE_ATTR, resource_name)
    # Host-readable diagnostics contain counts only, never names or content.
    resource.cognee_sync_stats = stats
    return resource
