"""Terraform Cloud connector for cognee — a ``dlt`` source that turns your infra runs into memory.

Pull HCP Terraform / Terraform Cloud (TFC) **runs** — their outcome, commit
message, and (redacted) plan log — into cognee, incrementally and with
forget-on-deletion. Like the sibling Confluence connector this builds entirely
on the existing DLT ingestion subsystem; the source produced here is handed
straight to :func:`cognee.remember`::

    import cognee
    from cognee_community_connector_terraform_cloud import terraform_cloud_source

    await cognee.remember(
        terraform_cloud_source(
            organization="my-org",
            token="…",                 # or TFC_TOKEN in the environment
            workspace_names=["prod"],  # omit to sync every workspace in the org
        ),
        dataset_name="terraform",
        primary_key="id",
        write_disposition="merge",   # incremental upsert by run id
        max_rows_per_table=0,        # 0 = no row cap (see note below)
    )

Design
------
* **Auth** — a Terraform Cloud API token, sent as ``Authorization: Bearer …``.
  Access is read-only — the connector only issues ``GET`` requests. The token
  falls back to the ``TFC_TOKEN`` / ``TERRAFORM_CLOUD_TOKEN`` environment
  variables.
* **Record** — one row per **run**, keyed by the TFC run id. A run is immutable
  once created, so combined with ``write_disposition="merge"`` ingestion is
  idempotent and a run is never re-fetched.
* **Incremental cursor** — the run ``created-at`` timestamp. Each run lists the
  most recent runs per workspace and emits only runs created after the highest
  timestamp seen so far (plus any run new to the corpus, e.g. from a
  workspace just added to the selection). The cursor is persisted in dlt's
  per-resource state, so re-running ``remember`` resumes where it left off.
* **Forget-on-delete** — deletion is scoped to the **workspace** disappearing.
  TFC has no deletion feed, so each run re-enumerates the organization's
  workspaces; any run whose workspace is gone is emitted with the ``_deleted``
  hard-delete marker. dlt removes those rows on ``merge`` and cognee's existing
  ``orphan_cleanup`` then purges them from the graph + vector + relational
  stores. Scoping deletion to a vanished workspace (rather than to a run falling
  out of the recent-runs window) means old runs ageing past
  ``max_runs_per_workspace`` are never mistaken for deletions.
* **Secret redaction** — plan logs routinely echo provider credentials,
  ``TF_VAR_*`` values, and connection strings. Every plan log is passed through
  :func:`redact_secrets` before it is emitted, so secrets never reach the graph
  or the embedding model. This is a deliberate, tested scrubbing step, not an
  afterthought.

.. note::
   cognee's ``ingest_dlt_source`` reads at most ``max_rows_per_table`` rows from
   the dlt destination (default 50). For a real organization pass
   ``max_rows_per_table=0`` (unlimited) so orphan-cleanup compares against the
   *whole* synced corpus rather than a truncated window.
"""

from __future__ import annotations

import os
import re
import time
from collections.abc import Iterator
from typing import Any

from cognee.shared.logging_utils import get_logger

logger = get_logger("terraform_cloud_connector")

# HCP Terraform / Terraform Cloud REST API v2.
_DEFAULT_BASE_URL = "https://app.terraform.io/api/v2"
# Public app URL (for building human-facing run links in each row).
_APP_URL = "https://app.terraform.io/app"

# How many of the most recent runs to enumerate per workspace each sync. Runs
# older than this window are not (re-)ingested, but they are never treated as
# deletions either (deletion is keyed on the workspace vanishing).
_DEFAULT_MAX_RUNS_PER_WORKSPACE = 100
# Cap a single plan log so one enormous apply cannot blow up ingestion.
_DEFAULT_MAX_PLAN_LOG_CHARS = 20_000

# Retry budget for rate-limited / transient TFC responses.
_MAX_RETRIES = 5

_EXTRA_HINT = (
    'The Terraform Cloud connector requires the dlt extra and "requests": '
    'pip install "cognee[terraform-cloud]".'
)


# ---------------------------------------------------------------------------
# Secret redaction — scrub plan logs before anything reaches memory
# ---------------------------------------------------------------------------
_REDACTED = "[REDACTED]"

# A PEM private key block, in full — the value is the whole block.
_PRIVATE_KEY_RE = re.compile(
    r"-----BEGIN (?:[A-Z]+ )?PRIVATE KEY-----.*?-----END (?:[A-Z]+ )?PRIVATE KEY-----",
    re.DOTALL,
)
# AWS access key id (fixed, recognisable shape).
_AWS_ACCESS_KEY_RE = re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")
# Credentials embedded in a URL: scheme://user:password@host → redact password.
_URL_CRED_RE = re.compile(r"(?P<pre>[a-zA-Z][a-zA-Z0-9+.-]*://[^:/\s@]+:)[^@/\s]+@")
# key = value / "key": value where the key name looks sensitive. The value may
# be single/double quoted or bare; only the value is replaced so the log still
# reads naturally (``api_key = [REDACTED]``).
_SENSITIVE_ASSIGN_RE = re.compile(
    r"""(?ix)
    (                                   # group 1: the key + operator (kept)
      "?[\w.\-]*
      (?: secret | password | passwd | pwd | token
        | api[_-]?key | access[_-]?key | secret[_-]?key | private[_-]?key
        | client[_-]?secret | credential | auth )
      [\w.\-]* "?
      \s* [:=] \s*
    )
    ( "[^"]*" | '[^']*' | \S+ )         # group 2: the value (redacted)
    """,
)


def redact_secrets(text: str | None) -> str:
    """Scrub secrets from Terraform plan/apply output before ingestion.

    Terraform marks *declared* sensitive attributes as ``(sensitive value)``
    itself, but provider credentials, ``TF_VAR_*`` echoes, connection strings,
    and raw key material routinely slip into log output. This removes the common
    shapes — PEM private keys, AWS access-key ids, URL-embedded passwords, and
    ``<sensitive-name> = <value>`` assignments — replacing each with
    ``[REDACTED]`` while leaving the surrounding log readable.

    It is intentionally conservative: it targets recognisable secret shapes and
    sensitively-named assignments rather than guessing at high-entropy strings,
    so ordinary resource addresses and plan diffs are left intact.
    """
    if not text:
        return ""
    text = _PRIVATE_KEY_RE.sub("[REDACTED PRIVATE KEY]", text)
    text = _AWS_ACCESS_KEY_RE.sub(_REDACTED, text)
    text = _URL_CRED_RE.sub(lambda m: f"{m.group('pre')}{_REDACTED}@", text)
    text = _SENSITIVE_ASSIGN_RE.sub(lambda m: f"{m.group(1)}{_REDACTED}", text)
    return text


# ---------------------------------------------------------------------------
# Auth / HTTP helpers
# ---------------------------------------------------------------------------
def _make_session(token: str) -> Any:
    """Build a ``requests`` session authenticated with a TFC API token.

    ``requests`` is imported lazily so it stays an optional dependency
    (``pip install "cognee[terraform-cloud]"``).
    """
    try:
        import requests
    except ImportError as exc:  # pragma: no cover - depends on optional extra
        raise ImportError(_EXTRA_HINT) from exc

    session = requests.Session()
    session.headers.update(
        {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/vnd.api+json",
        }
    )
    return session


def _is_transient(exc: Exception) -> bool:
    """True for rate-limit / server / network errors worth retrying."""
    try:
        import requests
    except ImportError:  # pragma: no cover
        return False
    if isinstance(exc, requests.exceptions.RequestException):
        response = getattr(exc, "response", None)
        if response is None:
            return True  # connection/timeout error, no response
        return response.status_code in (429, 500, 502, 503, 504)
    return False


def _request(method, *args, **kwargs):
    """Call a ``requests`` method, retrying rate-limit / transient errors.

    TFC enforces per-token rate limits (HTTP 429 with ``Retry-After``); without
    backoff a busy organization would abort mid-sync. Rate-limit, server, and
    network errors are retried with backoff; permanent errors (auth, not-found)
    and exhausted retries propagate so the caller can decide.
    """
    for attempt in range(_MAX_RETRIES):
        try:
            response = method(*args, **kwargs)
            response.raise_for_status()
            return response
        except Exception as exc:
            if attempt == _MAX_RETRIES - 1 or not _is_transient(exc):
                raise
            delay = _retry_after(getattr(exc, "response", None), attempt)
            logger.warning(
                "Terraform Cloud: %s — retrying in %.1fs (%d/%d).",
                exc,
                delay,
                attempt + 1,
                _MAX_RETRIES,
            )
            time.sleep(delay)


def _retry_after(response: Any, attempt: int) -> float:
    """Seconds to wait before retrying: the Retry-After header, else backoff."""
    header = getattr(response, "headers", None) or {}
    try:
        return float(header.get("Retry-After"))
    except (TypeError, ValueError):
        return float(2**attempt)


def _api_get(session: Any, base_url: str, path_or_url: str, params: dict | None = None) -> dict:
    """GET a TFC API path (or a ready-made pagination URL) and return JSON."""
    url = path_or_url if path_or_url.startswith("http") else f"{base_url}{path_or_url}"
    return _request(session.get, url, params=params or {}).json()


def _get_text(session: Any, url: str) -> str:
    """GET a raw text resource (e.g. a plan's pre-signed log URL)."""
    return _request(session.get, url).text


def _paginate(session: Any, base_url: str, path: str, params: dict) -> Iterator[dict]:
    """Yield JSON:API ``data`` items across pages, following ``links.next``.

    The ``next`` link is a fully-formed URL that already carries the page
    cursor, so subsequent requests drop the initial query params.
    """
    next_url: str | None = path
    next_params = dict(params)
    while next_url:
        payload = _api_get(session, base_url, next_url, next_params)
        yield from payload.get("data", []) or []
        next_url = (payload.get("links") or {}).get("next")
        next_params = {}


# ---------------------------------------------------------------------------
# TFC API reads
# ---------------------------------------------------------------------------
def _list_workspaces(session: Any, base_url: str, organization: str) -> list[dict]:
    """Return the organization's workspaces as JSON:API resource objects."""
    return list(
        _paginate(
            session,
            base_url,
            f"/organizations/{organization}/workspaces",
            {"page[size]": 100},
        )
    )


def _iter_recent_runs(
    session: Any, base_url: str, workspace_id: str, max_runs: int
) -> Iterator[dict]:
    """Yield up to ``max_runs`` most-recent runs for a workspace (newest first)."""
    runs = _paginate(session, base_url, f"/workspaces/{workspace_id}/runs", {"page[size]": 100})
    for count, run in enumerate(runs):
        if count >= max_runs:
            return
        yield run


def _plan_log(session: Any, base_url: str, run: dict, max_chars: int) -> str:
    """Fetch, cap, and redact a run's plan log (best-effort).

    The run → plan id comes from the run's relationships; ``GET /plans/:id``
    returns a short-lived ``log-read-url`` that serves the raw plan output. A
    failure here is non-fatal: the run's outcome is still worth ingesting, and
    because runs are immutable we log the gap rather than aborting the whole
    sync over one unreadable log.
    """
    plan_id = (((run.get("relationships") or {}).get("plan") or {}).get("data") or {}).get("id")
    if not plan_id:
        return ""
    try:
        plan = _api_get(session, base_url, f"/plans/{plan_id}")
        log_url = ((plan.get("data") or {}).get("attributes") or {}).get("log-read-url")
        if not log_url:
            return ""
        raw = _get_text(session, log_url)
    except Exception as exc:
        logger.warning(
            "Terraform Cloud: could not read plan log for run %s: %s", run.get("id"), exc
        )
        return ""
    if len(raw) > max_chars:
        raw = raw[:max_chars] + "\n…[plan log truncated]"
    return redact_secrets(raw)


# ---------------------------------------------------------------------------
# Row shaping
# ---------------------------------------------------------------------------
def _run_created_at(run: dict) -> str:
    """Return the run's ``created-at`` timestamp (the incremental cursor)."""
    return (run.get("attributes") or {}).get("created-at") or ""


def _run_to_row(run: dict, workspace: dict, organization: str, plan_log: str) -> dict[str, Any]:
    """Flatten a run (+ its workspace context and redacted plan log) into a row."""
    attrs = run.get("attributes") or {}
    ws_attrs = workspace.get("attributes") or {}
    ws_name = ws_attrs.get("name") or ""
    run_id = str(run.get("id"))

    body = _render_run(run_id, ws_name, organization, attrs, ws_attrs, plan_log)

    return {
        "id": run_id,
        "workspace": ws_name,
        "workspace_id": str(workspace.get("id") or ""),
        "organization": organization,
        "status": attrs.get("status") or "",
        "message": attrs.get("message") or "",
        "created_at": _run_created_at(run),
        "url": f"{_APP_URL}/{organization}/workspaces/{ws_name}/runs/{run_id}",
        "body": body,
        # Hard-delete marker (always False for live runs). Runs whose workspace
        # has vanished are emitted separately with _deleted=True.
        "_deleted": False,
    }


def _render_run(
    run_id: str,
    ws_name: str,
    organization: str,
    attrs: dict,
    ws_attrs: dict,
    plan_log: str,
) -> str:
    """Render a run to a readable markdown document for entity extraction."""
    lines = [
        f"# Terraform run {run_id}",
        f"Organization: {organization}",
        f"Workspace: {ws_name}",
        f"Status: {attrs.get('status') or 'unknown'}",
    ]
    if attrs.get("created-at"):
        lines.append(f"Created at: {attrs['created-at']}")
    if ws_attrs.get("terraform-version"):
        lines.append(f"Terraform version: {ws_attrs['terraform-version']}")
    if attrs.get("message"):
        lines.append(f"\nMessage: {attrs['message']}")
    if plan_log:
        lines.append(f"\n## Plan output\n\n```\n{plan_log}\n```")
    return "\n".join(lines)


def _deleted_row(run_id: str) -> dict[str, Any]:
    """Build a minimal row that instructs dlt to hard-delete a run by id."""
    return {"id": str(run_id), "_deleted": True}


# ---------------------------------------------------------------------------
# Sync (pure given a session + state dict — unit-testable)
# ---------------------------------------------------------------------------
def sync_runs(
    session: Any,
    base_url: str,
    organization: str,
    state: dict,
    *,
    workspace_names: list[str] | None = None,
    include_plan_logs: bool = True,
    max_runs_per_workspace: int = _DEFAULT_MAX_RUNS_PER_WORKSPACE,
    max_plan_log_chars: int = _DEFAULT_MAX_PLAN_LOG_CHARS,
) -> Iterator[dict[str, Any]]:
    """Yield runs created since the last sync, plus hard-delete markers.

    One workspace-listing pass drives both ingestion and deletion detection:
    runs created after the stored cursor (or new to the corpus) are rendered and
    emitted, while runs whose workspace has disappeared are emitted as
    hard-delete markers. The cursor (``last_created_at``) and the run→workspace
    map (``known_runs``) are advanced in ``state`` so the next sync is a no-op
    when nothing changed.
    """
    known_runs: dict[str, str] = dict(state.get("known_runs", {}))
    last_created_at: str = state.get("last_created_at", "")
    newest_created_at = last_created_at

    wanted = set(workspace_names) if workspace_names else None
    current_ws_ids: set[str] = set()
    changed = 0

    for workspace in _list_workspaces(session, base_url, organization):
        ws_id = str(workspace.get("id") or "")
        ws_name = (workspace.get("attributes") or {}).get("name") or ""
        if wanted is not None and ws_name not in wanted:
            continue
        current_ws_ids.add(ws_id)

        for run in _iter_recent_runs(session, base_url, ws_id, max_runs_per_workspace):
            run_id = str(run.get("id"))
            created_at = _run_created_at(run)
            # Skip runs we have already ingested. A run is immutable, so a known
            # id never needs re-fetching; a run NOT yet known is ingested
            # regardless of timestamp (it may predate the cursor — e.g. a
            # workspace just added to the selection).
            if run_id in known_runs:
                continue
            if created_at > newest_created_at:
                newest_created_at = created_at

            plan_log = (
                _plan_log(session, base_url, run, max_plan_log_chars) if include_plan_logs else ""
            )
            yield _run_to_row(run, workspace, organization, plan_log)
            known_runs[run_id] = ws_id
            changed += 1

    # Deletion detection relies on the workspace sweep succeeding. An empty sweep
    # while workspaces were previously known almost always means a transient
    # failure (network blip, token scope change) rather than a genuine wipe —
    # treating it as "everything deleted" would purge the whole dataset and
    # overwrite known_runs permanently. Skip deletion and preserve state.
    if known_runs and not current_ws_ids:
        logger.warning(
            "Terraform Cloud: workspace sweep returned 0 workspaces but %d run(s) were "
            "known; skipping deletion this run to avoid a mass forget-on-delete on a "
            "transient sweep.",
            len(known_runs),
        )
        state["last_created_at"] = newest_created_at
        logger.info("Terraform Cloud: %d new run(s), 0 deletion(s).", changed)
        return

    # A run whose workspace is gone from the org is forgotten. Runs that merely
    # aged out of the recent-runs window keep their workspace, so they survive.
    deleted = [run_id for run_id, ws_id in known_runs.items() if ws_id not in current_ws_ids]
    for run_id in sorted(deleted):
        known_runs.pop(run_id, None)
        yield _deleted_row(run_id)

    state["known_runs"] = known_runs
    state["last_created_at"] = newest_created_at
    logger.info("Terraform Cloud: %d new run(s), %d deletion(s).", changed, len(deleted))


# ---------------------------------------------------------------------------
# Public factory
# ---------------------------------------------------------------------------
def terraform_cloud_source(
    *,
    organization: str,
    token: str | None = None,
    workspace_names: list[str] | None = None,
    base_url: str = _DEFAULT_BASE_URL,
    include_plan_logs: bool = True,
    max_runs_per_workspace: int = _DEFAULT_MAX_RUNS_PER_WORKSPACE,
    max_plan_log_chars: int = _DEFAULT_MAX_PLAN_LOG_CHARS,
    session: Any = None,
):
    """Return a ``dlt`` resource that yields Terraform Cloud runs for ``remember``.

    Args:
        organization: TFC organization name to sync.
        token: TFC API token. Falls back to ``TFC_TOKEN`` /
            ``TERRAFORM_CLOUD_TOKEN``.
        workspace_names: Restrict to these workspace names. ``None`` syncs every
            workspace the token can read in the organization.
        base_url: API base, defaults to HCP Terraform
            (``https://app.terraform.io/api/v2``); set this for Terraform
            Enterprise.
        include_plan_logs: Fetch and (redacted) attach each run's plan log.
        max_runs_per_workspace: Cap the most-recent runs enumerated per
            workspace each sync.
        max_plan_log_chars: Truncate a single plan log to this many characters.
        session: Pre-built ``requests`` session. Mainly an injection point for
            tests; when omitted one is built from ``token``.

    Returns:
        A ``dlt`` resource (``terraform_cloud_runs``) configured with
        ``primary_key="id"``, ``write_disposition="merge"`` and an ``_deleted``
        hard-delete column. Hand it to ``cognee.remember(...)``.
    """
    try:
        import dlt
    except ImportError as exc:
        raise ImportError(_EXTRA_HINT) from exc

    base_url = base_url.rstrip("/")
    resolved_token = token or os.environ.get("TFC_TOKEN") or os.environ.get("TERRAFORM_CLOUD_TOKEN")
    if session is None and not resolved_token:
        raise ValueError(
            "terraform_cloud_source requires a token (pass token= or set TFC_TOKEN), "
            "or an injected session."
        )

    @dlt.resource(
        name="terraform_cloud_runs",
        primary_key="id",
        write_disposition="merge",
        # _deleted is a boolean hard-delete marker: rows where it is True are
        # removed from the dlt destination on merge, which propagates the
        # deletion through cognee's orphan_cleanup.
        columns={"_deleted": {"data_type": "bool", "hard_delete": True}},
    )
    def terraform_cloud_runs():
        client = session or _make_session(resolved_token)
        resource_state = dlt.current.resource_state()
        yield from sync_runs(
            client,
            base_url,
            organization,
            resource_state,
            workspace_names=workspace_names,
            include_plan_logs=include_plan_logs,
            max_runs_per_workspace=max_runs_per_workspace,
            max_plan_log_chars=max_plan_log_chars,
        )

    return terraform_cloud_runs
