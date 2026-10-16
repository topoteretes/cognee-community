"""Terraform Cloud connector for cognee: a ``dlt`` source that turns HCP Terraform into memory.

Sync an HCP Terraform (Terraform Cloud) organization's workspaces, run outcomes
and plan output into cognee, incrementally and with forget-on-delete. The
resource is handed directly to :func:`cognee.remember`::

    import cognee
    from cognee_community_connector_terraform_cloud import terraform_cloud_source

    await cognee.remember(
        terraform_cloud_source(organization="acme"),  # TFE_TOKEN from env
        dataset_name="terraform",
        primary_key="id",
        write_disposition="merge",  # REQUIRED, the add pipeline defaults to "replace"
        max_rows_per_table=0,
    )

Design
------
* **Rows** are flat ``{id, title, content, url, _deleted}``: one per workspace
  (``workspace:<id>``) and one per run (``run:<id>``). Workspace documents skip
  fields that change on every run (resource count, lock state), so they are
  only re-processed when the workspace itself changes.
* **Incremental** with a run ``created-at`` cursor. The run list endpoints allow
  30 requests per minute and have no ``created-at`` filter, so runs are listed
  for the whole organization, 100 per page, and paging stops once runs are
  older than the cursor (as long as the API keeps returning them newest first).
  A run is ingested once it reaches a final state; runs still in progress are
  kept in state and checked again on the next sync.
* **Plan output** is a summary read from the plan log: the resource addresses
  with their action, the ``Plan: ...`` line and the diagnostics. Attribute
  values are never kept, and every text field goes through the scrubbing step
  in :mod:`.redact` before it is emitted.
* **Forget-on-delete.** Runs cannot be deleted on their own, so a workspace that
  is deleted, no longer visible or no longer selected is emitted as a hard-delete
  tombstone together with all of its runs. Any other failure raises, and dlt
  rolls the state of a failed run back.

Privacy
-------
Plans and logs describe your infrastructure. Nothing is fetched until you
construct a source and call ``remember``. Use a read-only team token, and
remember that anyone with read access to the target dataset can read what was
ingested.
"""

import hashlib
import json
import logging
import os
import re
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from cognee.tasks.ingestion import dlt_utils
from cognee.tasks.ingestion.dlt_utils import DOCUMENT_SOURCE_ATTR

from cognee_community_connector_terraform_cloud.redact import scrub, strip_ansi

logger = logging.getLogger(__name__)

DEFAULT_HOSTNAME = "app.terraform.io"
DEFAULT_SINCE_DAYS = 90
_PAGE_SIZE = 100
_TIMEOUT = 30.0
_RETRIES = 6
_MAX_RETRY_SLEEP = 60.0
# A margin against runs created in the same second as the cursor.
_CURSOR_MARGIN = timedelta(minutes=5)
_NAMES_PER_REQUEST = 25
_MAX_CHANGES = 200
_RUN_INCLUDE = "plan,apply,configuration_version.ingress_attributes"
_OPERATIONS = "plan_and_apply,save_plan,refresh_only,destroy,empty_apply,action_only"
# Run states with no further transitions. planned_and_saved can still be applied.
_FINAL_STATES = {
    "applied",
    "planned_and_finished",
    "policy_soft_failed",
    "discarded",
    "errored",
    "canceled",
    "force_canceled",
}
_ACTION_WORDS = {
    "create": "will be created",
    "update": "will be updated in-place",
    "delete": "will be destroyed",
    "replace": "must be replaced",
    "read": "will be read during apply",
    "import": "will be imported",
    "move": "has moved",
}
_TEXT_CHANGE = re.compile(
    r"^# (\S+) (will be created|will be updated in-place|will be destroyed|must be replaced"
    r"|will be read during apply|will be imported|has moved to \S+)"
)
_TEXT_SUMMARY = re.compile(r"^(Plan: .*|No changes\..*|Apply complete!.*)$")
_TEXT_DIAGNOSTIC = re.compile(r"^(Error|Warning): (.+)$")


# ---------------------------------------------------------------------------
# Errors: messages carry the HTTP status only, never the token, URLs or bodies.
# ---------------------------------------------------------------------------
class TerraformCloudSourceError(RuntimeError):
    """Base class of the errors this source raises."""


class TerraformCloudAuthError(TerraformCloudSourceError):
    """HCP Terraform rejected the token (HTTP 401)."""


class TerraformCloudAPIError(TerraformCloudSourceError):
    """HCP Terraform answered with an error, or could not be reached."""

    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


# ---------------------------------------------------------------------------
# Transport
# ---------------------------------------------------------------------------
def _retry_delay(response: Any, attempt: int) -> float:
    for header in ("Retry-After", "X-RateLimit-Reset"):
        try:
            return min(max(float(response.headers.get(header)), 0.0), _MAX_RETRY_SLEEP)
        except (TypeError, ValueError):
            continue
    return min(float(2**attempt), _MAX_RETRY_SLEEP)


class TerraformCloudClient:
    """Minimal synchronous client for the HCP Terraform API (JSON:API).

    ``get`` returns the decoded JSON document and ``download`` the text behind a
    blob storage URL. Tests and hosts can pass any object with the same two
    methods to :func:`terraform_cloud_source` instead.
    """

    def __init__(
        self,
        token: str,
        hostname: str = DEFAULT_HOSTNAME,
        *,
        http: Any = None,
        sleep: Callable[[float], None] = time.sleep,
    ):
        token = (token or "").strip()
        # Reject early and without echoing the value: httpx would put an invalid
        # header value, token included, into its exception.
        if not token or not token.isascii() or not token.isprintable() or " " in token:
            raise ValueError("The HCP Terraform token is empty or has an invalid format")
        import httpx

        self.api_url = f"https://{hostname}/api/v2"
        self._headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/vnd.api+json",
        }
        self._http = http or httpx.Client(timeout=_TIMEOUT)
        self._sleep = sleep

    def __repr__(self) -> str:
        return "TerraformCloudClient(token=<redacted>)"

    def _send(self, url: str, params: dict[str, Any] | None, headers: dict[str, str]) -> Any:
        import httpx

        for attempt in range(_RETRIES):
            last = attempt == _RETRIES - 1
            try:
                response = self._http.get(url, params=params, headers=headers)
            except httpx.TransportError as exc:
                if last:
                    raise TerraformCloudAPIError(
                        f"HCP Terraform request failed: {type(exc).__name__}"
                    ) from None
                self._sleep(float(2**attempt))
                continue
            status = response.status_code
            if (status == 429 or status >= 500) and not last:
                self._sleep(_retry_delay(response, attempt))
                continue
            if status == 401:
                raise TerraformCloudAuthError("HCP Terraform rejected the token (HTTP 401)")
            if status != 200:
                raise TerraformCloudAPIError(
                    f"HCP Terraform request failed: HTTP {status}", status=status
                )
            return response
        raise TerraformCloudAPIError("HCP Terraform request failed")  # pragma: no cover

    def get(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        return self._send(self.api_url + path, params, self._headers).json()

    def download(self, url: str) -> str:
        # Blob storage URLs carry their own secret and must not get the token.
        return self._send(url, None, {}).text


# ---------------------------------------------------------------------------
# Plan and apply logs: resource-level summary, never attribute values.
# ---------------------------------------------------------------------------
def summarize_log(text: str) -> list[str]:
    """Planned changes, the summary line and diagnostics from a plan or apply log.

    Handles structured run output (one JSON object per line) and plain text
    logs with ANSI colors and the box characters around diagnostics.
    """
    changes: list[str] = []
    summary: list[str] = []
    diagnostics: list[str] = []
    for raw in strip_ansi(text).splitlines():
        line = raw.strip().lstrip("╷│╵").strip()
        if line.startswith("{"):
            try:
                event = json.loads(line)
            except ValueError:
                event = None
            if isinstance(event, dict):
                _structured_event(event, changes, summary, diagnostics)
                continue
        if match := _TEXT_CHANGE.match(line):
            changes.append(f"{match.group(1)} {match.group(2)}")
        elif _TEXT_SUMMARY.match(line):
            summary.append(line)
        elif match := _TEXT_DIAGNOSTIC.match(line):
            diagnostics.append(f"{match.group(1)}: {match.group(2)}")

    lines = []
    if changes:
        extra = len(changes) - _MAX_CHANGES
        lines.extend(["Planned changes:", *(f"- {c}" for c in changes[:_MAX_CHANGES])])
        if extra > 0:
            lines.append(f"- ... and {extra} more")
    lines.extend(dict.fromkeys(summary))
    if diagnostics:
        lines.extend(["Diagnostics:", *dict.fromkeys(diagnostics)])
    return lines


def _structured_event(event: dict, changes: list, summary: list, diagnostics: list) -> None:
    kind = event.get("type")
    if kind == "planned_change":
        change = event.get("change") or {}
        address = (change.get("resource") or {}).get("addr")
        words = _ACTION_WORDS.get(change.get("action"))
        if address and words:
            changes.append(f"{address} {words}")
    elif kind == "change_summary" and event.get("@message"):
        summary.append(str(event["@message"]))
    elif kind == "diagnostic":
        diagnostic = event.get("diagnostic") or {}
        severity = str(diagnostic.get("severity") or "error").capitalize()
        if diagnostic.get("summary"):
            diagnostics.append(f"{severity}: {diagnostic['summary']}")


# ---------------------------------------------------------------------------
# Rendering: deterministic, no volatile fields, every text scrubbed.
# ---------------------------------------------------------------------------
def _scrubbed(row: dict[str, Any], stats: dict[str, int]) -> dict[str, Any]:
    for key in ("title", "content"):
        row[key], found = scrub(row[key])
        stats["redacted"] += found
    return row


def render_workspace(workspace: dict, organization: str, hostname: str) -> dict[str, Any]:
    attributes = workspace.get("attributes") or {}
    name = str(attributes.get("name") or workspace["id"])
    vcs = attributes.get("vcs-repo") or {}
    repository = vcs.get("identifier") or attributes.get("vcs-repo-identifier") or ""
    if repository and vcs.get("branch"):
        repository += f" (branch {vcs['branch']})"
    fields = [
        ("Workspace", name),
        ("Organization", organization),
        ("Terraform version", attributes.get("terraform-version")),
        ("Execution mode", attributes.get("execution-mode")),
        ("Repository", repository),
        ("Working directory", attributes.get("working-directory")),
        ("Auto apply", "yes" if attributes.get("auto-apply") else ""),
        ("Tags", ", ".join(sorted(attributes.get("tag-names") or []))),
    ]
    parts = [f"{label}: {value}" for label, value in fields if value]
    description = str(attributes.get("description") or "").strip()
    if description:
        parts.extend(["", description])
    return {
        "id": f"workspace:{workspace['id']}",
        "title": f"Workspace {name}",
        "content": "\n".join(parts),
        "url": f"https://{hostname}/app/{organization}/workspaces/{name}",
        "_deleted": False,
    }


def _operation(attributes: dict) -> str:
    if attributes.get("is-destroy"):
        return "destroy"
    if attributes.get("refresh-only"):
        return "refresh only"
    if attributes.get("plan-only"):
        return "plan only"
    if attributes.get("save-plan"):
        return "saved plan"
    return "plan and apply"


def _counts(item: dict | None, verbs: tuple[str, str, str]) -> str:
    attributes = (item or {}).get("attributes") or {}
    if attributes.get("status") not in ("finished", "errored"):
        return ""
    keys = ("resource-additions", "resource-changes", "resource-destructions")
    return ", ".join(f"{int(attributes.get(k) or 0)} {v}" for k, v in zip(keys, verbs, strict=True))


def render_run(
    run: dict,
    workspace_name: str,
    related: dict[str, dict],
    log_lines: list[str],
    organization: str,
    hostname: str,
) -> dict[str, Any]:
    attributes = run.get("attributes") or {}
    ingress = related.get("ingress") or {}
    ingress_attributes = ingress.get("attributes") or {}
    commit = str(ingress_attributes.get("commit-sha") or "")[:12]
    if commit and ingress_attributes.get("branch"):
        commit += f" on {ingress_attributes['branch']}"
    commit_message = str(ingress_attributes.get("commit-message") or "").strip()
    if commit and commit_message:
        commit += f": {commit_message.splitlines()[0]}"
    created = str(attributes.get("created-at") or "")
    fields = [
        ("Workspace", workspace_name),
        ("Status", attributes.get("status")),
        ("Operation", _operation(attributes)),
        ("Trigger", attributes.get("trigger-reason")),
        ("Source", attributes.get("source")),
        ("Created", created[:16].replace("T", " ") + " UTC" if created else ""),
        ("Message", str(attributes.get("message") or "").strip()),
        ("Commit", commit),
        ("Plan", _counts(related.get("plan"), ("to add", "to change", "to destroy"))),
        ("Apply", _counts(related.get("apply"), ("added", "changed", "destroyed"))),
    ]
    parts = [f"{label}: {value}" for label, value in fields if value]
    if log_lines:
        parts.extend(["", *log_lines])
    return {
        "id": f"run:{run['id']}",
        "title": f"{workspace_name} run {run['id']}",
        "content": "\n".join(parts),
        "url": f"https://{hostname}/app/{organization}/workspaces/{workspace_name}/runs/{run['id']}",
        "_deleted": False,
    }


def _digest(row: dict) -> str:
    return hashlib.md5(json.dumps(row, sort_keys=True).encode()).hexdigest()


# ---------------------------------------------------------------------------
# Extraction
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class _TerraformCloudConfig:
    organization: str
    hostname: str
    workspace_names: tuple[str, ...]
    workspace_wildcard: str | None
    workspace_tags: tuple[str, ...]
    project_id: str | None
    include_speculative: bool
    include_plan_output: bool
    since_days: int

    @property
    def filtered(self) -> bool:
        return bool(
            self.workspace_names
            or self.workspace_wildcard
            or self.workspace_tags
            or self.project_id
        )


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _pages(client: Any, path: str, params: dict[str, Any]) -> Iterator[dict]:
    """Yield JSON:API documents page by page."""
    page = 1
    while True:
        document = client.get(path, {**params, "page[number]": page, "page[size]": _PAGE_SIZE})
        yield document
        if not (document.get("links") or {}).get("next") or not document.get("data"):
            return
        page += 1


def _workspaces(client: Any, config: _TerraformCloudConfig) -> dict[str, dict]:
    params: dict[str, Any] = {}
    if config.workspace_wildcard:
        params["search[wildcard-name]"] = config.workspace_wildcard
    if config.workspace_tags:
        params["search[tags]"] = ",".join(config.workspace_tags)
    if config.project_id:
        params["filter[project][id]"] = config.project_id
    path = f"/organizations/{config.organization}/workspaces"
    wanted = set(config.workspace_names)
    workspaces = {}
    for document in _pages(client, path, params):
        for workspace in document.get("data") or []:
            name = (workspace.get("attributes") or {}).get("name")
            if not wanted or name in wanted:
                workspaces[str(workspace["id"])] = workspace
    return workspaces


def _related(run: dict, included: dict[tuple[str, str], dict]) -> dict[str, dict]:
    def lookup(item: dict | None, name: str) -> dict | None:
        data = (((item or {}).get("relationships") or {}).get(name) or {}).get("data") or {}
        return included.get((data.get("type"), data.get("id")))

    version = lookup(run, "configuration-version")
    related = {
        "plan": lookup(run, "plan"),
        "apply": lookup(run, "apply"),
        "ingress": lookup(version, "ingress-attributes"),
    }
    return {name: item for name, item in related.items() if item}


def _included(document: dict) -> dict[tuple[str, str], dict]:
    return {(item.get("type"), item.get("id")): item for item in document.get("included") or []}


def _workspace_id(run: dict) -> str | None:
    data = (((run.get("relationships") or {}).get("workspace") or {}).get("data")) or {}
    return data.get("id")


def _list_runs(
    client: Any, config: _TerraformCloudConfig, names: list[str], floor: str
) -> Iterator[tuple[dict, dict[str, dict]]]:
    """Runs created at or after ``floor``, with their plan, apply and commit."""
    operations = _OPERATIONS + (",plan_only" if config.include_speculative else "")
    params: dict[str, Any] = {"include": _RUN_INCLUDE, "filter[operation]": operations}
    groups = [names[i : i + _NAMES_PER_REQUEST] for i in range(0, len(names), _NAMES_PER_REQUEST)]
    for group in groups or [None]:
        if group is not None:
            params["filter[workspace_names]"] = ",".join(group)
        previous = None
        ordered = True
        for document in _pages(client, f"/organizations/{config.organization}/runs", params):
            included = _included(document)
            stop = False
            for run in document.get("data") or []:
                created = str((run.get("attributes") or {}).get("created-at") or "")
                # Stop early only while the API keeps answering newest first.
                ordered = ordered and (previous is None or created <= previous)
                previous = created
                if created < floor:
                    stop = True
                    continue
                yield run, _related(run, included)
            if stop and ordered:
                break


def _log_lines(client: Any, related: dict[str, dict], run: dict) -> list[str]:
    lines = []
    for name in ("plan", "apply"):
        attributes = (related.get(name) or {}).get("attributes") or {}
        url = attributes.get("log-read-url")
        # The apply log only adds something when the apply itself failed.
        if url and (name == "plan" or attributes.get("status") == "errored"):
            lines.extend(summarize_log(client.download(url)))
    return list(dict.fromkeys(lines))


def _iter_rows(
    client: Any,
    config: _TerraformCloudConfig,
    state: dict,
    stats: dict[str, int],
    *,
    now: Callable[[], datetime] | None = None,
) -> Iterator[dict]:
    """Yield changed rows and tombstones. Pure of dlt, so tests drive it with a dict."""
    organization, hostname = config.organization, config.hostname
    try:
        client.get(f"/organizations/{organization}")
    except TerraformCloudAPIError as exc:
        if exc.status == 404:
            raise TerraformCloudAPIError(
                "HCP Terraform organization not found or not visible to this token", status=404
            ) from None
        raise

    workspaces = _workspaces(client, config)
    known: dict[str, dict] = state.setdefault("workspaces", {})
    pending: dict[str, str] = state.setdefault("pending", {})
    names = {
        wid: str((w.get("attributes") or {}).get("name") or wid) for wid, w in workspaces.items()
    }

    rows: list[dict] = []
    for wid, workspace in workspaces.items():
        entry = known.setdefault(wid, {"hash": None, "runs": []})
        row = _scrubbed(render_workspace(workspace, organization, hostname), stats)
        if _digest(row) != entry["hash"]:
            entry["hash"] = _digest(row)
            rows.append(row)

    cursor = state.get("cursor")
    if cursor:
        floor = (datetime.fromisoformat(cursor.replace("Z", "+00:00")) - _CURSOR_MARGIN).isoformat()
    else:
        floor = ((now or _utcnow)() - timedelta(days=config.since_days)).isoformat()
    floor = floor.replace("+00:00", "Z")
    selected_names = sorted(names.values()) if config.filtered else []

    def emit(run: dict, related: dict[str, dict]) -> None:
        wid = _workspace_id(run)
        logs = _log_lines(client, related, run) if config.include_plan_output else []
        row = render_run(run, names[wid], related, logs, organization, hostname)
        rows.append(_scrubbed(row, stats))
        known[wid]["runs"].append(run["id"])
        pending.pop(run["id"], None)

    newest = cursor
    listed: set[str] = set()
    for run, related in _list_runs(client, config, selected_names, floor):
        listed.add(run["id"])
        wid = _workspace_id(run)
        created = str((run.get("attributes") or {}).get("created-at") or "")
        newest = max(newest or created, created)
        if wid not in workspaces or run["id"] in known[wid]["runs"]:
            continue
        if (run.get("attributes") or {}).get("status") in _FINAL_STATES:
            emit(run, related)
        else:
            pending[run["id"]] = wid

    # Runs that were still in progress last time and are now below the cursor.
    for run_id, wid in list(pending.items()):
        if run_id in listed:
            continue
        if wid not in workspaces:
            pending.pop(run_id)
            continue
        try:
            document = client.get(f"/runs/{run_id}", {"include": _RUN_INCLUDE})
        except TerraformCloudAPIError as exc:
            if exc.status != 404:
                raise
            pending.pop(run_id)
            continue
        run = document["data"]
        if (run.get("attributes") or {}).get("status") in _FINAL_STATES:
            emit(run, _related(run, _included(document)))

    # Deleted, hidden or deselected workspaces: forget the workspace and its runs.
    for wid in [wid for wid in known if wid not in workspaces]:
        entry = known.pop(wid)
        rows.extend({"id": f"run:{run_id}", "_deleted": True} for run_id in entry["runs"])
        rows.append({"id": f"workspace:{wid}", "_deleted": True})

    state["cursor"] = newest
    stats["emitted"] = sum(1 for row in rows if not row.get("_deleted"))
    stats["deleted"] = len(rows) - stats["emitted"]
    stats["pending"] = len(pending)
    logger.info(
        "HCP Terraform: %d row(s) emitted, %d deleted, %d run(s) in progress.",
        stats["emitted"],
        stats["deleted"],
        stats["pending"],
    )
    yield from rows


# ---------------------------------------------------------------------------
# Public factory
# ---------------------------------------------------------------------------
def terraform_cloud_source(
    organization: str | None = None,
    *,
    token: str | None = None,
    hostname: str | None = None,
    workspace_names: list[str] | None = None,
    workspace_wildcard: str | None = None,
    workspace_tags: list[str] | None = None,
    project_id: str | None = None,
    include_speculative: bool = False,
    include_plan_output: bool = True,
    since_days: int = DEFAULT_SINCE_DAYS,
    resource_name: str = "terraform_cloud",
    check_active: Callable[[], None] | None = None,
    client: Any = None,
):
    """Return a ``dlt`` resource that yields one document per workspace and per run.

    Hand the result to ``cognee.remember(...)`` with ``write_disposition="merge"``
    and ``primary_key="id"``.

    Args:
        organization: Organization name (``TFE_ORGANIZATION``).
        token: User or team API token (``TFE_TOKEN``). A read-only team token is enough.
        hostname: ``app.terraform.io`` (default), ``app.eu.terraform.io`` or a Terraform
            Enterprise host (``TFE_HOSTNAME``).
        workspace_names: Exact workspace names to sync.
        workspace_wildcard: Name pattern with ``*``, for example ``"*-prod"``.
        workspace_tags: Only workspaces with all of these tags.
        project_id: Only workspaces of this project.
        include_speculative: Also ingest plan-only (speculative) runs.
        include_plan_output: Add the plan summary (resource changes, summary line,
            diagnostics) to each run.
        since_days: How far back the first sync reads runs.
        resource_name: Staging table name. Use a different one per sync scope
            that shares a dataset.
        check_active: Optional host authorization checkpoint, called around each row.
        client: Pre-built client (see :class:`TerraformCloudClient`). Mainly an
            injection point for tests and hosts.

    Returns:
        A ``dlt`` resource configured with ``primary_key="id"``,
        ``write_disposition="merge"`` and a ``_deleted`` hard-delete column.
    """
    try:
        import dlt
    except ImportError as exc:
        raise ImportError(
            'The Terraform Cloud connector requires dlt: pip install "cognee[dlt]".'
        ) from exc

    if getattr(dlt_utils, "DOCUMENT_SYNC_VERSION", 0) < 1:
        raise RuntimeError(
            "Terraform Cloud sync requires a cognee build with table-scoped DLT document cleanup."
        )

    resolved_org = organization or os.getenv("TFE_ORGANIZATION")
    if not resolved_org:
        raise ValueError("organization is required (pass it explicitly or set TFE_ORGANIZATION).")
    resolved_host = hostname or os.getenv("TFE_HOSTNAME") or DEFAULT_HOSTNAME
    if client is None:
        resolved_token = token or os.getenv("TFE_TOKEN")
        if not resolved_token:
            raise ValueError("An API token is required (pass token= or set TFE_TOKEN).")
        client = TerraformCloudClient(resolved_token, resolved_host)

    config = _TerraformCloudConfig(
        organization=resolved_org,
        hostname=resolved_host,
        workspace_names=tuple(workspace_names or ()),
        workspace_wildcard=workspace_wildcard,
        workspace_tags=tuple(workspace_tags or ()),
        project_id=project_id,
        include_speculative=include_speculative,
        include_plan_output=include_plan_output,
        since_days=int(since_days),
    )
    stats: dict[str, int] = {}

    @dlt.resource(
        name=resource_name,
        primary_key="id",
        write_disposition="merge",
        columns={"_deleted": {"data_type": "bool", "hard_delete": True}},
    )
    def terraform_cloud():
        stats.clear()
        stats.update(emitted=0, deleted=0, pending=0, redacted=0)
        rows = _iter_rows(client, config, dlt.current.resource_state(), stats)
        yield from dlt_utils.guarded_rows(rows, check_active)

    resource = terraform_cloud()
    setattr(resource, DOCUMENT_SOURCE_ATTR, "terraform_cloud")
    setattr(resource, dlt_utils.PIPELINE_SCOPE_ATTR, resource_name)
    resource.cognee_sync_stats = stats
    return resource
