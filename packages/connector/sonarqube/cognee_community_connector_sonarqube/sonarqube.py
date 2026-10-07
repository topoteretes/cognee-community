"""SonarQube connector for cognee — a ``dlt`` source that turns code-quality state into memory.

Sync SonarQube (or SonarCloud) — issues, security hotspots, and quality-gate
outcomes — into cognee, incrementally and with forget-on-deletion — "ask my
code quality".  Like the sibling Confluence connector this builds entirely on
the existing DLT ingestion subsystem; the source produced here is handed
directly to :func:`cognee.remember`::

    import cognee
    from cognee_community_connector_sonarqube import sonarqube_source

    await cognee.remember(
        sonarqube_source(
            base_url="https://sonarqube.example.com",
            token="…",                      # or SONARQUBE_TOKEN
            project_keys=["org_repo"],      # None = every visible project
            min_severity="MAJOR",           # skip INFO/MINOR noise
        ),
        dataset_name="code_quality",
        primary_key="id",
        write_disposition="merge",   # incremental upsert by issue/hotspot key
        max_rows_per_table=0,        # 0 = no row cap (see note below)
    )

Design
------
* **Auth** — a SonarQube user token, sent as ``Authorization: Bearer`` on every
  request (works for SonarCloud too — just point ``base_url`` at
  ``https://sonarcloud.io``). The connector only issues ``GET`` requests.
* **Documents** — one document per issue (message, rule, severity, type,
  status, component, tags), one per security hotspot, and one per project
  carrying its quality-gate outcome and last analysis date. Each links back to
  the SonarQube web UI.
* **Primary key** — the SonarQube issue/hotspot key (project key for the
  quality-gate document). Combined with ``write_disposition="merge"`` this
  gives idempotent upserts, and cognee's content-hash ``data_id`` keeps
  unchanged documents from being re-cognified.
* **Incremental cursor** — ``createdAfter`` on the issues search, per the
  issue spec: each run fetches only issues created since the highest
  ``creationDate`` stored for the project. The cursor is persisted in dlt's
  per-resource state, so re-running ``remember`` resumes where it left off.
  Status changes are caught by the deletion sweep below (a resolved issue
  leaves the unresolved listing), which is also how in-place metadata edits
  that SonarQube cannot filter on are bounded: re-reads are content-hashed,
  so they are no-ops downstream.
* **Forget-on-delete** — each run does a keys-only sweep of every fetched
  project's *unresolved* issues: known issues that left the unresolved set
  (resolved or deleted upstream) are emitted with the ``_deleted``
  hard-delete marker. Hotspots are swept in full each run (they are few):
  hotspots marked ``REVIEWED`` or removed are tombstoned. Projects that
  vanish from the project listing (or from ``project_keys``) tombstone all
  their documents. A project that fails to fetch is skipped for the run — its
  documents are never tombstoned on unseen evidence.

.. note::
   cognee's ``ingest_dlt_source`` reads at most ``max_rows_per_table`` rows
   from the dlt destination (default 50). For real projects pass
   ``max_rows_per_table=0`` (unlimited) so orphan-cleanup compares against the
   *whole* synced corpus rather than a truncated window.

.. note::
   Large projects can carry tens of thousands of issues. Use ``min_severity``
   (the connector filters to unresolved issues at/above it) and prefer
   explicit ``project_keys`` to bound each sync.
"""

from __future__ import annotations

import hashlib
import os
from collections.abc import Iterator
from typing import Any

from cognee.shared.logging_utils import get_logger
from cognee.tasks.ingestion.dlt_utils import DOCUMENT_SOURCE_ATTR

logger = get_logger("sonarqube_connector")

# dlt resource / staging-table name, and the system_metadata["source"] tag
# stamped on every document this connector produces.
SONARQUBE_SOURCE_NAME = "sonarqube"
SONARQUBE_TABLE_NAME = "sonarqube_documents"

# Issue severities, low → high (SonarQube's own scale).
_SEVERITY_ORDER = ["INFO", "MINOR", "MAJOR", "CRITICAL", "BLOCKER"]

_PAGE_SIZE = 500


# ---------------------------------------------------------------------------
# Auth / HTTP helpers
# ---------------------------------------------------------------------------
def _make_session(token: str) -> Any:
    """Build a ``requests`` session authenticated with a SonarQube user token.

    ``requests`` is imported lazily so it stays an optional dependency.
    """
    try:
        import requests
    except ImportError as exc:  # pragma: no cover - depends on optional extra
        raise ImportError(
            'The SonarQube connector requires "requests". Install the connector:\n'
            '    pip install "cognee-community-connector-sonarqube"'
        ) from exc

    session = requests.Session()
    session.headers["Authorization"] = f"Bearer {token}"
    return session


def _api_get(session: Any, base_url: str, path: str, params: dict | None = None) -> Any:
    """GET a SonarQube Web API path and return the decoded JSON."""
    response = session.get(f"{base_url.rstrip('/')}{path}", params=params or {})
    response.raise_for_status()
    return response.json()


def _paginate(session: Any, base_url: str, path: str, params: dict, items_key: str) -> list[dict]:
    """Yield all pages of a Web API list endpoint (``p``/``ps`` pagination)."""
    items: list[dict] = []
    page = 1
    while True:
        data = _api_get(session, base_url, path, {**params, "p": page, "ps": _PAGE_SIZE})
        batch = data.get(items_key) or []
        items.extend(item for item in batch if isinstance(item, dict))
        total = (data.get("paging") or {}).get("total", len(items))
        if page * _PAGE_SIZE >= total or not batch:
            break
        page += 1
    return items


# ---------------------------------------------------------------------------
# Document rendering
# ---------------------------------------------------------------------------
def _content_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _issue_content(issue: dict) -> str:
    lines = [issue.get("message") or f"Issue {issue.get('rule')} on {issue.get('component')}"]
    lines.append("")
    lines.append(f"Rule: {issue.get('rule') or 'unknown'}")
    lines.append(f"Severity: {issue.get('severity') or 'UNKNOWN'}")
    lines.append(f"Type: {issue.get('type') or 'UNKNOWN'}")
    lines.append(f"Status: {issue.get('status') or 'UNKNOWN'}")
    if issue.get("component"):
        lines.append(f"Component: {issue['component']}")
    if issue.get("creationDate"):
        lines.append(f"Created: {issue['creationDate']}")
    tags = ", ".join(issue.get("tags") or [])
    if tags:
        lines.append(f"Tags: {tags}")
    return "\n".join(lines)


def _hotspot_content(hotspot: dict) -> str:
    lines = [hotspot.get("message") or f"Security hotspot {hotspot.get('key')}"]
    lines.append("")
    lines.append(f"Rule: {hotspot.get('ruleKey') or 'unknown'}")
    probability = hotspot.get("vulnerabilityProbability") or "UNKNOWN"
    lines.append(f"Vulnerability probability: {probability}")
    lines.append(f"Status: {hotspot.get('status') or 'UNKNOWN'}")
    if hotspot.get("component"):
        lines.append(f"Component: {hotspot['component']}")
    return "\n".join(lines)


def _quality_gate_content(project: dict, status: dict) -> str:
    if not status or not status.get("status"):
        # No quality gate configured for this project: no document to emit.
        return ""
    lines = [f"Quality gate: {status['status']}"]
    lines.append("")
    for condition in status.get("conditions") or []:
        lines.append(
            "- {metric}: {status} (value: {value}, threshold: {error})".format(
                metric=condition.get("metric") or "?",
                status=condition.get("status") or "?",
                value=condition.get("value") or "-",
                error=condition.get("errorThreshold") or "-",
            )
        )
    if project.get("lastAnalysisDate"):
        lines.append("")
        lines.append(f"Last analysis: {project['lastAnalysisDate']}")
    return "\n".join(lines)


def _row(row_id: str, title: str, content: str, url: str) -> dict[str, Any]:
    return {"id": row_id, "title": title, "content": content, "url": url, "_deleted": False}


def _deleted_row(item_id: str) -> dict[str, Any]:
    """Build a minimal row that instructs dlt to hard-delete a document by id."""
    return {"id": item_id, "_deleted": True}


def _issue_url(base_url: str, issue: dict) -> str:
    project = issue.get("project") or ""
    key = issue.get("key") or ""
    return f"{base_url.rstrip('/')}/project/issues?id={project}&open={key}&issues={key}"


def _hotspot_url(base_url: str, hotspot: dict) -> str:
    project = hotspot.get("project") or ""
    key = hotspot.get("key") or ""
    return f"{base_url.rstrip('/')}/security_hotspots?id={project}&hotspots={key}"


# ---------------------------------------------------------------------------
# Sync (pure given a session + state dict — unit-testable)
# ---------------------------------------------------------------------------
def sync_projects(
    session: Any,
    base_url: str,
    state: dict,
    *,
    project_keys: list[str] | None = None,
    min_severity: str | None = None,
    stats: dict[str, int] | None = None,
) -> Iterator[dict[str, Any]]:
    """Yield new/changed issue, hotspot, and quality-gate documents, plus hard-delete markers.

    ``createdAfter`` on the issues search (per project) drives new-issue
    pickup; a keys-only sweep of unresolved issues drives resolution and
    deletion detection; hotspots and quality-gate documents are swept in full
    (both are small and content-hashed). All state is advanced in ``state`` so
    the next run is a no-op when nothing changed. A project whose fetch fails
    is skipped for the run — its documents are neither emitted nor tombstoned
    on that evidence.
    """
    if stats is None:
        stats = {}
    stats.clear()
    stats.update(synced_projects=0, skipped_projects=0, emitted=0, deleted=0)

    if min_severity is not None and min_severity not in _SEVERITY_ORDER:
        raise ValueError(f"min_severity must be one of {_SEVERITY_ORDER}, got {min_severity!r}.")
    severities = _SEVERITY_ORDER[_SEVERITY_ORDER.index(min_severity) :] if min_severity else None

    known_projects: dict[str, dict] = dict(state.get("projects", {}))
    known_issues: dict[str, dict] = dict(state.get("issues", {}))
    known_hotspots: dict[str, dict] = dict(state.get("hotspots", {}))

    try:
        projects = _paginate(session, base_url, "/api/projects/search", {}, "components")
    except Exception as exc:
        logger.warning("SonarQube: skipping sync (project listing failed): %s", exc)
        state.update(projects=known_projects, issues=known_issues, hotspots=known_hotspots)
        return

    visible = {
        project["key"]: project
        for project in projects
        if project.get("key") and project.get("qualifier", "TRK") == "TRK"
    }
    wanted = list(project_keys) if project_keys else sorted(visible)
    synced: set[str] = set()
    skipped: set[str] = set()

    for project_key in wanted:
        project = visible.get(project_key)
        if project is None:
            # Unknown key: leave its state untouched (maybe transient), but do
            # not sync it — the removal sweep below only fires for projects
            # that were *previously known* and are now absent from the
            # listing, so a typo cannot silently wipe memory.
            if project_key in known_projects:
                logger.warning(
                    "SonarQube: project %s is no longer visible; forgetting its documents.",
                    project_key,
                )
                for item_id, meta in list(known_issues.items()):
                    if meta.get("project") == project_key:
                        known_issues.pop(item_id)
                        stats["deleted"] += 1
                        yield _deleted_row(item_id)
                for item_id, meta in list(known_hotspots.items()):
                    if meta.get("project") == project_key:
                        known_hotspots.pop(item_id)
                        stats["deleted"] += 1
                        yield _deleted_row(item_id)
                known_projects.pop(project_key, None)
                stats["deleted"] += 1
                yield _deleted_row(project_key)  # quality-gate document
            else:
                logger.warning("SonarQube: project %s not found, skipping.", project_key)
            continue

        project_state = dict(known_projects.get(project_key, {}))
        project_issues = {
            key: meta for key, meta in known_issues.items() if meta.get("project") == project_key
        }
        project_hotspots = {
            key: meta for key, meta in known_hotspots.items() if meta.get("project") == project_key
        }
        try:
            # Quality-gate outcome document (hash-gated; skipped when the
            # project has no gate configured).
            try:
                gate_status = (
                    _api_get(
                        session,
                        base_url,
                        "/api/qualitygates/project_status",
                        {"projectKey": project_key},
                    ).get("projectStatus")
                    or {}
                )
            except Exception:
                gate_status = {}
            gate_content = _quality_gate_content(project, gate_status)
            gate_hash = _content_hash(gate_content)
            gate_url = f"{base_url.rstrip('/')}/dashboard?id={project_key}"

            # New issues since the cursor (createdAfter, unresolved only).
            issue_params: dict[str, Any] = {"componentKeys": project_key, "resolved": "false"}
            if severities:
                issue_params["severities"] = ",".join(severities)
            if project_state.get("last_created"):
                issue_params["createdAfter"] = project_state["last_created"]
            issues = _paginate(session, base_url, "/api/issues/search", issue_params, "issues")

            # Keys-only sweep of unresolved issues: drives resolution/deletion.
            unresolved_keys = {
                issue["key"]
                for issue in _paginate(
                    session,
                    base_url,
                    "/api/issues/search",
                    {"componentKeys": project_key, "resolved": "false", "fields": "key"},
                    "issues",
                )
                if issue.get("key")
            }

            # Security hotspots are few; full sweep each run.
            hotspots = _paginate(
                session, base_url, "/api/hotspots/search", {"project": project_key}, "hotspots"
            )
        except Exception as exc:
            stats["skipped_projects"] += 1
            skipped.add(project_key)
            logger.warning("SonarQube: skipping project %s (fetch failed): %s", project_key, exc)
            continue

        synced.add(project_key)
        if gate_content and gate_hash != project_state.get("gate_hash"):
            stats["emitted"] += 1
            yield _row(project_key, project.get("name") or project_key, gate_content, gate_url)
            project_state["gate_hash"] = gate_hash
        elif not gate_content and project_state.get("gate_hash"):
            # The project's quality gate was removed upstream: forget its
            # quality-gate document.
            stats["deleted"] += 1
            yield _deleted_row(project_key)
            project_state.pop("gate_hash", None)

        for issue in issues:
            key = issue.get("key")
            if not key:
                continue
            content = _issue_content(issue)
            content_hash = _content_hash(content)
            previous = known_issues.get(key)
            if previous is None or content_hash != previous.get("hash"):
                stats["emitted"] += 1
                yield _row(
                    key,
                    (issue.get("message") or f"{issue.get('severity')} issue")[:120],
                    content,
                    _issue_url(base_url, issue),
                )
            known_issues[key] = {"hash": content_hash, "project": project_key}
            if issue.get("creationDate"):
                previous_cursor = project_state.get("last_created") or ""
                if issue["creationDate"] > previous_cursor:
                    project_state["last_created"] = issue["creationDate"]

        # Deletion detection (issues): a known issue that left the unresolved
        # set is resolved or deleted upstream — emit the hard-delete marker so
        # dlt's merge drops it and cognee's orphan_cleanup forgets it.
        for key in sorted(set(project_issues) - unresolved_keys):
            known_issues.pop(key, None)
            stats["deleted"] += 1
            yield _deleted_row(key)

        for hotspot in hotspots:
            key = hotspot.get("key")
            if not key:
                continue
            content = _hotspot_content(hotspot)
            content_hash = _content_hash(content)
            known_hotspots[key] = {"hash": content_hash, "project": project_key}
            if hotspot.get("status") == "REVIEWED":
                # A reviewed hotspot is resolved upstream: forget it.
                known_hotspots.pop(key, None)
                stats["deleted"] += 1
                yield _deleted_row(key)
                continue
            previous = project_hotspots.get(key)
            if previous is None or content_hash != previous.get("hash"):
                stats["emitted"] += 1
                yield _row(
                    key,
                    (hotspot.get("message") or "Security hotspot")[:120],
                    content,
                    _hotspot_url(base_url, hotspot),
                )
        for key in sorted(set(project_hotspots) - {h.get("key") for h in hotspots if h.get("key")}):
            known_hotspots.pop(key, None)
            stats["deleted"] += 1
            yield _deleted_row(key)

        known_projects[project_key] = project_state
        stats["synced_projects"] += 1

    # Projects dropped from the configuration (absent from the listing and
    # from project_keys, and not merely skipped by a transient failure): their
    # documents are no longer wanted.
    dropped = {
        key
        for key in set(known_projects)
        | {m.get("project") for m in known_issues.values()}
        | {m.get("project") for m in known_hotspots.values()}
        if key not in synced and key not in skipped and key not in visible
    }
    for key, meta in list(known_issues.items()):
        if meta.get("project") in dropped:
            known_issues.pop(key, None)
            stats["deleted"] += 1
            yield _deleted_row(key)
    for key, meta in list(known_hotspots.items()):
        if meta.get("project") in dropped:
            known_hotspots.pop(key, None)
            stats["deleted"] += 1
            yield _deleted_row(key)
    for key in known_projects:
        if key in dropped:
            stats["deleted"] += 1
            yield _deleted_row(key)  # quality-gate document
    known_projects = {key: s for key, s in known_projects.items() if key not in dropped}

    state["projects"] = known_projects
    state["issues"] = known_issues
    state["hotspots"] = known_hotspots
    logger.info(
        "SonarQube: %d project(s) synced, %d document(s) emitted, %d deletion(s), "
        "%d project(s) skipped.",
        stats["synced_projects"],
        stats["emitted"],
        stats["deleted"],
        stats["skipped_projects"],
    )


# ---------------------------------------------------------------------------
# Public factory
# ---------------------------------------------------------------------------
def sonarqube_source(
    *,
    base_url: str,
    token: str | None = None,
    project_keys: list[str] | None = None,
    min_severity: str | None = None,
    resource_name: str = SONARQUBE_TABLE_NAME,
    session: Any = None,
):
    """Return a ``dlt`` resource that yields SonarQube documents for ``remember``.

    Args:
        base_url: SonarQube base URL (e.g. ``https://sonarqube.example.com``);
            use ``https://sonarcloud.io`` for SonarCloud.
        token: SonarQube user token. Falls back to ``SONARQUBE_TOKEN``.
        project_keys: Restrict to these project keys. ``None`` syncs every
            project the token can see.
        min_severity: Only ingest unresolved issues at/above this severity
            (``INFO`` < ``MINOR`` < ``MAJOR`` < ``CRITICAL`` < ``BLOCKER``).
            Recommended for large projects.
        resource_name: Stable dlt resource name. dlt state is keyed per
            resource name, so hosts syncing several servers into one dataset
            should give each its own name.
        session: Pre-built ``requests`` session. Mainly an injection point for
            tests; when omitted one is built from ``token``.

    Returns:
        A ``dlt`` resource (``sonarqube_documents``) configured with
        ``primary_key="id"``, ``write_disposition="merge"`` and an ``_deleted``
        hard-delete column. Hand it to ``cognee.remember(...)``.
    """
    try:
        import dlt
    except ImportError as exc:
        raise ImportError(
            "The SonarQube connector requires dlt. Install it with the connector:\n"
            '    pip install "cognee-community-connector-sonarqube"'
        ) from exc

    token = token or os.environ.get("SONARQUBE_TOKEN")
    if session is None and not token:
        raise ValueError("sonarqube_source requires token (or an injected session).")
    if not base_url or not base_url.startswith(("http://", "https://")):
        raise ValueError("base_url must be an http(s) URL.")

    stats: dict[str, int] = {}

    @dlt.resource(
        name=resource_name,
        primary_key="id",
        write_disposition="merge",
        # _deleted is a boolean hard-delete marker (matching gmail/confluence):
        # rows where it is True are removed from the dlt destination on merge,
        # which propagates the deletion through cognee's orphan_cleanup.
        columns={"_deleted": {"data_type": "bool", "hard_delete": True}},
    )
    def sonarqube_documents():
        client = session or _make_session(token)
        resource_state = dlt.current.resource_state()
        yield from sync_projects(
            client,
            base_url,
            resource_state,
            project_keys=project_keys,
            min_severity=min_severity,
            stats=stats,
        )

    resource = sonarqube_documents()
    # Opt into the document ingestion path: each row (id/title/content/url)
    # becomes a text document that flows through normal cognify (LLM graph
    # extraction). resolve_dlt_sources reads this marker; it never imports
    # this connector.
    setattr(resource, DOCUMENT_SOURCE_ATTR, SONARQUBE_SOURCE_NAME)
    # Host-readable diagnostics contain counts only, never issue content.
    resource.cognee_sync_stats = stats
    return resource
