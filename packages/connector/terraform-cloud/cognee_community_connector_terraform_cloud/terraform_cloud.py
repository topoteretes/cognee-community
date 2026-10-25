"""Terraform Cloud connector for cognee: a ``dlt`` source that turns HCP Terraform into memory.

Sync an HCP Terraform (Terraform Cloud) organization's workspaces, run outcomes
and plan output into cognee, incrementally and with forget-on-delete. The
resource is handed directly to :func:`cognee.remember`::

    import cognee
    from cognee_community_connector_terraform_cloud import terraform_cloud_source

    await cognee.remember(
        terraform_cloud_source(organization="acme", token=token),
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
  tombstone together with all of its runs. A workspace listing that comes back
  empty is confirmed by the next run before anything is forgotten.
* **A run is bounded.** A long rate limit, a spent retry wait budget or a spent
  request budget stops the run cleanly, keeps the runs it finished and reports
  why in ``cognee_sync_stats``. Any other failure raises, and dlt rolls the state
  of a failed run back.

Privacy
-------
Plans and logs describe your infrastructure. Nothing is fetched until you
construct a source and call ``remember``. Use a read-only team token, and
remember that anyone with read access to the target dataset can read what was
ingested.
"""

import hashlib
import json
import re
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from cognee.shared.logging_utils import get_logger
from cognee.tasks.ingestion import dlt_utils
from cognee.tasks.ingestion.dlt_utils import DOCUMENT_SOURCE_ATTR

from cognee_community_connector_terraform_cloud.redact import scrub, strip_ansi

logger = get_logger("terraform_cloud_connector")

DEFAULT_HOSTNAME = "app.terraform.io"
# The first sync reads about one quarter of run history.
DEFAULT_SINCE_DAYS = 90
# Total time one run may wait on retries before it stops cleanly.
DEFAULT_MAX_SLEEP = 300.0
# Requests per run; bigger organizations finish over several runs.
DEFAULT_MAX_REQUESTS = 1000
# Largest page[size] the API accepts.
_PAGE_SIZE = 100
# The API usually answers in well under a second; only a stuck connection hits this.
_TIMEOUT = 30.0
# Attempts per request; backing off 1, 2, 4, 8 and 16 seconds gives up after about 30,
# which rides out one window of the 30 per minute limit on run listings.
_RETRIES = 6
# Rate limit windows are one minute; a longer Retry-After stops the run.
_MAX_RETRY_SLEEP = 60.0
# A margin against runs created in the same second as the cursor.
_CURSOR_MARGIN = timedelta(minutes=5)
# Workspace names per run listing, so the filter stays well inside URL length limits.
_NAMES_PER_REQUEST = 25
# Planned changes kept per run; the rest is summarized as a count.
_MAX_CHANGES = 200
# Only a single run can include its plan and apply: the organization run listing
# answers HTTP 400 "Invalid include parameter" for them, so it includes nothing.
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
_STATS = ("emitted", "deleted", "pending", "redacted", "failed")


# ---------------------------------------------------------------------------
# Errors: messages carry the HTTP status only, never the token, URLs or bodies.
# ---------------------------------------------------------------------------
class TerraformCloudSourceError(RuntimeError):
    """Base class of the errors this source raises."""


class TerraformCloudAuthError(TerraformCloudSourceError):
    """HCP Terraform rejected the token (HTTP 401)."""


class TerraformCloudRateLimitedError(TerraformCloudSourceError):
    """HCP Terraform kept answering 429, asked for a long wait, or the wait budget is spent."""


class TerraformCloudAPIError(TerraformCloudSourceError):
    """HCP Terraform answered with an error, or could not be reached."""

    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


# ---------------------------------------------------------------------------
# Transport
# ---------------------------------------------------------------------------
def _retry_after(response: Any) -> float | None:
    for header in ("Retry-After", "X-RateLimit-Reset"):
        try:
            return max(float(response.headers.get(header)), 0.0)
        except (TypeError, ValueError):
            continue
    return None


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
        max_sleep: float = DEFAULT_MAX_SLEEP,
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
        self._max_sleep = max_sleep
        self._slept = 0.0

    def __repr__(self) -> str:
        return "TerraformCloudClient(token=<redacted>)"

    def reset_sleep_budget(self) -> None:
        """Start a new run's retry wait budget."""
        self._slept = 0.0

    def _wait(self, delay: float) -> None:
        if self._slept + delay > self._max_sleep:
            raise TerraformCloudRateLimitedError(
                "HCP Terraform retries used up the wait budget of this run"
            )
        self._slept += delay
        self._sleep(delay)

    def _send(self, url: str, params: dict[str, Any] | None, headers: dict[str, str]) -> Any:
        import httpx

        attempt = 0
        while True:
            last = attempt == _RETRIES - 1
            try:
                response = self._http.get(url, params=params, headers=headers)
            except httpx.TransportError as exc:
                if last:
                    # Not chained: log download URLs are signed, and httpx puts the URL
                    # into its exception.
                    raise TerraformCloudAPIError(
                        f"HCP Terraform request failed: {type(exc).__name__}"
                    ) from None
                self._wait(float(2**attempt))
                attempt += 1
                continue
            status = response.status_code
            if status == 429:
                wait = _retry_after(response)
                if last or (wait is not None and wait > _MAX_RETRY_SLEEP):
                    raise TerraformCloudRateLimitedError(
                        "HCP Terraform rate limit exceeded (HTTP 429)"
                    )
                self._wait(wait if wait is not None else float(2**attempt))
            elif status >= 500 and not last:
                wait = _retry_after(response)
                self._wait(min(wait if wait is not None else float(2**attempt), _MAX_RETRY_SLEEP))
            elif status == 401:
                raise TerraformCloudAuthError("HCP Terraform rejected the token (HTTP 401)")
            elif status != 200:
                raise TerraformCloudAPIError(
                    f"HCP Terraform request failed: HTTP {status}", status=status
                )
            else:
                return response
            attempt += 1

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
    max_requests: int = DEFAULT_MAX_REQUESTS

    @property
    def filtered(self) -> bool:
        return bool(
            self.workspace_names
            or self.workspace_wildcard
            or self.workspace_tags
            or self.project_id
        )


class _CutShortError(Exception):
    """Internal: stop the run here, keeping the runs finished so far."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


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


class _Walker:
    """One run over an organization, writing progress into ``state``."""

    def __init__(
        self,
        client: Any,
        config: _TerraformCloudConfig,
        state: dict,
        stats: dict[str, int],
        check_active: Callable[[], None] | None,
        now: datetime,
    ):
        self.client = client
        self.config = config
        self.state = state
        self.stats = stats
        self.check_active = check_active
        self.now = now
        self.known: dict[str, dict] = state.setdefault("workspaces", {})
        self.pending: dict[str, str] = state.setdefault("pending", {})
        self.requests = 0
        # Set once a run is emitted; the budget never stops a run before that.
        self.progressed = False
        # Rows produced so far, in step with the state changes made for them.
        self.rows: list[dict] = []

    def _call(self, method: Callable[..., Any], *args: Any) -> Any:
        if self.check_active is not None:
            self.check_active()
        if self.progressed and self.requests >= self.config.max_requests:
            raise _CutShortError("budget")
        self.requests += 1
        try:
            return method(*args)
        except TerraformCloudRateLimitedError:
            raise _CutShortError("rate_limit") from None

    def _pages(self, path: str, params: dict[str, Any]) -> Iterator[dict]:
        """Yield JSON:API documents page by page."""
        page = 1
        while True:
            query = {**params, "page[number]": page, "page[size]": _PAGE_SIZE}
            document = self._call(self.client.get, path, query)
            # A 200 without the list must not read as "nothing exists".
            if not isinstance(document.get("data"), list):
                raise TerraformCloudAPIError("HCP Terraform answered without a data list")
            yield document
            if not (document.get("links") or {}).get("next") or not document["data"]:
                return
            page += 1

    # -- workspaces --------------------------------------------------------
    def _workspaces(self) -> dict[str, dict]:
        config = self.config
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
        for document in self._pages(path, params):
            for workspace in document["data"]:
                name = (workspace.get("attributes") or {}).get("name")
                if not wanted or name in wanted:
                    workspaces[str(workspace["id"])] = workspace
        found = {(w.get("attributes") or {}).get("name") for w in workspaces.values()}
        synced = {entry.get("name") for entry in self.known.values()}
        never = sorted(wanted - found - synced)
        if never:
            # Never synced and not in the organization: most likely a typo.
            raise TerraformCloudSourceError(
                f"HCP Terraform workspace(s) {', '.join(never)} in workspace_names were not found"
            )
        return workspaces

    def _forget_unlisted(self, workspaces: dict[str, dict]) -> None:
        """Deleted, hidden or deselected workspaces: forget the workspace and its runs.
        Decided right after the listing, so a run cut short still forgets them."""
        gone = [wid for wid in self.known if wid not in workspaces]
        if gone and not workspaces and not self.state.get("empty_listing"):
            # A workspace listing that is empty as a whole is confirmed by the next run.
            self.state["empty_listing"] = True
            logger.warning(
                "HCP Terraform listed no workspaces although %d were synced before; nothing "
                "is forgotten until the next run confirms it.",
                len(gone),
            )
            return
        self.state.pop("empty_listing", None)
        for wid in gone:
            entry = self.known.pop(wid)
            self.rows.extend({"id": f"run:{run_id}", "_deleted": True} for run_id in entry["runs"])
            self.rows.append({"id": f"workspace:{wid}", "_deleted": True})

    # -- runs --------------------------------------------------------------
    def _list_runs(self, names: list[str], floor: str) -> Iterator[dict]:
        """Runs created at or after ``floor``."""
        config = self.config
        operations = _OPERATIONS + (",plan_only" if config.include_speculative else "")
        params: dict[str, Any] = {"filter[operation]": operations}
        size = _NAMES_PER_REQUEST
        groups = [names[i : i + size] for i in range(0, len(names), size)]
        for group in groups or [None]:
            if group is not None:
                params["filter[workspace_names]"] = ",".join(group)
            previous = None
            ordered = True
            for document in self._pages(f"/organizations/{config.organization}/runs", params):
                stop = False
                for run in document["data"]:
                    created = str((run.get("attributes") or {}).get("created-at") or "")
                    # Stop early only while the API keeps answering newest first.
                    ordered = ordered and (previous is None or created <= previous)
                    previous = created
                    if created < floor:
                        stop = True
                        continue
                    yield run
                if stop and ordered:
                    break

    def _log_lines(self, related: dict[str, dict]) -> list[str]:
        lines = []
        for name in ("plan", "apply"):
            attributes = (related.get(name) or {}).get("attributes") or {}
            url = attributes.get("log-read-url")
            # The apply log only adds something when the apply itself failed.
            if url and (name == "plan" or attributes.get("status") == "errored"):
                lines.extend(summarize_log(self._call(self.client.download, url)))
        return list(dict.fromkeys(lines))

    def _emit(self, run: dict, related: dict[str, dict], names: dict[str, str]) -> None:
        config = self.config
        wid = _workspace_id(run)
        logs = self._log_lines(related) if config.include_plan_output else []
        row = render_run(run, names[wid], related, logs, config.organization, config.hostname)
        self.rows.append(_scrubbed(row, self.stats))
        self.known[wid]["runs"].append(run["id"])
        self.pending.pop(run["id"], None)
        self.progressed = True

    def _read_run(self, run_id: str) -> dict | None:
        """One run with its plan, apply and commit, or ``None`` when it is gone."""
        try:
            return self._call(self.client.get, f"/runs/{run_id}", {"include": _RUN_INCLUDE})
        except TerraformCloudAPIError as exc:
            if exc.status != 404:
                raise
            return None

    # -- the run -----------------------------------------------------------
    def walk(self) -> None:
        """Fill ``self.rows``; the cursor only moves once the whole walk is done."""
        config = self.config
        organization, hostname = config.organization, config.hostname
        try:
            self._call(self.client.get, f"/organizations/{organization}")
        except TerraformCloudAPIError as exc:
            if exc.status == 404:
                raise TerraformCloudAPIError(
                    "HCP Terraform organization not found or not visible to this token",
                    status=404,
                ) from None
            raise

        workspaces = self._workspaces()
        self._forget_unlisted(workspaces)
        names = {
            wid: str((w.get("attributes") or {}).get("name") or wid)
            for wid, w in workspaces.items()
        }
        for wid, workspace in workspaces.items():
            entry = self.known.setdefault(wid, {"hash": None, "runs": []})
            entry["name"] = names[wid]
            row = _scrubbed(render_workspace(workspace, organization, hostname), self.stats)
            if _digest(row) != entry["hash"]:
                entry["hash"] = _digest(row)
                self.rows.append(row)

        cursor = self.state.get("cursor")
        if cursor:
            moment = datetime.fromisoformat(cursor.replace("Z", "+00:00")) - _CURSOR_MARGIN
        else:
            moment = self.now - timedelta(days=config.since_days)
        floor = moment.isoformat().replace("+00:00", "Z")
        selected_names = sorted(names.values()) if config.filtered else []

        newest = cursor
        listed: set[str] = set()
        for run in self._list_runs(selected_names, floor):
            listed.add(run["id"])
            wid = _workspace_id(run)
            created = str((run.get("attributes") or {}).get("created-at") or "")
            newest = max(newest or created, created)
            if wid not in workspaces or run["id"] in self.known[wid]["runs"]:
                continue
            if (run.get("attributes") or {}).get("status") not in _FINAL_STATES:
                self.pending[run["id"]] = wid
                continue
            # The listing cannot include the plan and apply, so a finished run is read once.
            document = self._read_run(run["id"])
            if document is not None:
                self._emit(document["data"], _related(document["data"], _included(document)), names)

        # Runs that were still in progress last time and are now below the cursor.
        for run_id, wid in list(self.pending.items()):
            if run_id in listed:
                continue
            if wid not in workspaces:
                self.pending.pop(run_id)
                continue
            document = self._read_run(run_id)
            if document is None:
                self.pending.pop(run_id)
                continue
            run = document["data"]
            if (run.get("attributes") or {}).get("status") in _FINAL_STATES:
                self._emit(run, _related(run, _included(document)), names)

        self.state["cursor"] = newest


def _iter_rows(
    client: Any,
    config: _TerraformCloudConfig,
    state: dict,
    stats: dict[str, int],
    *,
    now: Callable[[], datetime] | None = None,
    check_active: Callable[[], None] | None = None,
) -> Iterator[dict]:
    """Yield changed rows and tombstones. Pure of dlt, so tests drive it with a dict."""
    for key in _STATS:
        stats.setdefault(key, 0)
    reset = getattr(client, "reset_sleep_budget", None)
    if reset is not None:
        reset()
    walker = _Walker(client, config, state, stats, check_active, (now or _utcnow)())
    try:
        walker.walk()
    except _CutShortError as cut:
        # A spent budget is a planned pause, not a failure; failed_budget says why.
        if cut.reason != "budget":
            stats["failed"] = 1
        stats[f"failed_{cut.reason}"] = 1
        logger.warning(
            "HCP Terraform sync stopped early (%s); it resumes on the next run.", cut.reason
        )
    rows = walker.rows
    stats["emitted"] = sum(1 for row in rows if not row.get("_deleted"))
    stats["deleted"] = len(rows) - stats["emitted"]
    stats["pending"] = len(walker.pending)
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
def _default_resource_name(organization: str, hostname: str) -> str:
    scope = organization if hostname == DEFAULT_HOSTNAME else f"{hostname}_{organization}"
    slug = re.sub(r"[^a-z0-9]+", "_", scope.lower()).strip("_")
    if slug != scope:
        # Keep names that differ only in case or punctuation apart.
        slug += "_" + hashlib.sha256(scope.encode()).hexdigest()[:8]
    return "terraform_cloud_" + slug


def terraform_cloud_source(
    organization: str,
    *,
    token: str | None = None,
    hostname: str = DEFAULT_HOSTNAME,
    workspace_names: list[str] | None = None,
    workspace_wildcard: str | None = None,
    workspace_tags: list[str] | None = None,
    project_id: str | None = None,
    include_speculative: bool = False,
    include_plan_output: bool = True,
    since_days: int = DEFAULT_SINCE_DAYS,
    max_requests: int = DEFAULT_MAX_REQUESTS,
    resource_name: str | None = None,
    check_active: Callable[[], None] | None = None,
    service: Any = None,
    client: Any = None,
):
    """Return a ``dlt`` resource that yields one document per workspace and per run.

    Hand the result to ``cognee.remember(...)`` with ``write_disposition="merge"``
    and ``primary_key="id"``.

    Args:
        organization: Organization name.
        token: User or team API token. A read-only team token is enough.
        hostname: ``app.terraform.io`` (default), ``app.eu.terraform.io`` or a Terraform
            Enterprise host.
        workspace_names: Exact workspace names to sync. A name that was never synced
            and is not in the organization raises.
        workspace_wildcard: Name pattern with ``*``, for example ``"*-prod"``.
        workspace_tags: Only workspaces with all of these tags.
        project_id: Only workspaces of this project.
        include_speculative: Also ingest plan-only (speculative) runs.
        include_plan_output: Add the plan summary (resource changes, summary line,
            diagnostics) to each run.
        since_days: How far back the first sync reads runs.
        max_requests: Request budget of one run, checked once a run is emitted. A
            bigger organization finishes over several runs; a spent budget sets
            ``failed_budget`` but not ``failed``.
        resource_name: Staging table name, which scopes this sync's state and
            cleanup. Defaults to one derived from the organization (and the host,
            when it is not the default).
        check_active: Optional host authorization checkpoint, called before each
            request and around each row.
        service: Pre-built client (see :class:`TerraformCloudClient`). Mainly an
            injection point for tests and hosts. ``client`` is accepted as an alias.

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

    if not organization:
        raise ValueError("organization is required.")
    service = service or client
    if service is None:
        if not token:
            raise ValueError("An API token is required: pass token=, or a pre-built service.")
        service = TerraformCloudClient(token=token, hostname=hostname)
    name = resource_name or _default_resource_name(organization, hostname)

    config = _TerraformCloudConfig(
        organization=organization,
        hostname=hostname,
        workspace_names=tuple(workspace_names or ()),
        workspace_wildcard=workspace_wildcard,
        workspace_tags=tuple(workspace_tags or ()),
        project_id=project_id,
        include_speculative=include_speculative,
        include_plan_output=include_plan_output,
        since_days=int(since_days),
        max_requests=int(max_requests),
    )
    stats: dict[str, int] = {}

    @dlt.resource(
        name=name,
        primary_key="id",
        write_disposition="merge",
        columns={"_deleted": {"data_type": "bool", "hard_delete": True}},
    )
    def terraform_cloud():
        stats.clear()
        state = dlt.current.resource_state()
        rows = _iter_rows(service, config, state, stats, check_active=check_active)
        yield from dlt_utils.guarded_rows(rows, check_active)

    resource = terraform_cloud()
    setattr(resource, DOCUMENT_SOURCE_ATTR, "terraform_cloud")
    setattr(resource, dlt_utils.PIPELINE_SCOPE_ATTR, name)
    resource.cognee_sync_stats = stats
    return resource
