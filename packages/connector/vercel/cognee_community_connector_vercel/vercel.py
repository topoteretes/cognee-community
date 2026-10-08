"""dlt source for Vercel projects, deployments and failed-build output.

Built on dlt's declarative ``rest_api`` source. It makes three calls and has no
hand-written HTTP:

* ``GET /v10/projects``, one document per project
* ``GET /v7/deployments``, one document per deployment
* ``GET /v3/deployments/{id}/events``, one document per failed build, requested
  only for deployments in state ``ERROR``

Why it is built this way:

Document path. The source sets ``cognee_document_source``, so every row is its
own text document with a content-hash id. One changed deployment re-processes
one document. On the relational path any change re-emits the whole source.

Allowlist, never a denylist. The project object Vercel returns embeds an ``env``
array (key, type, value), deploy-hook URLs under ``link.deployHooks`` and
protection-bypass data. Skipping the env endpoints is not enough to keep those
out. Each row is rebuilt from named fields in a ``map`` step before dlt writes
anything, so a field Vercel adds later stays out by default.

Full snapshot of a fixed window. ``since`` on the deployments list filters on
creation time, so a cursor that only moves forward never sees ``BUILDING`` turn
into ``ERROR`` and never sees a deletion. Each run re-reads every deployment
created in the last ``lookback_days`` under ``write_disposition="replace"``.
Unchanged rows keep their content hash and are not re-cognified. Rows that left
the snapshot (deleted, removed by retention, or older than the window) are
forgotten by cognee's ``orphan_cleanup``.

Fail closed. Staging is authoritative under replace, so a short read would be
forgotten as if it were a deletion. An HTTP error or an unexpected response
shape on a list call raises and aborts the run, which leaves memory untouched.

Build output is its own document. A ``rest_api`` child resource cannot write
back into its parent's row, so the log of a failed build is loaded as a
separate document keyed by the deployment id.
"""

import os
from datetime import UTC, datetime, timedelta
from itertools import groupby
from typing import Any

from cognee.tasks.ingestion.dlt_utils import DOCUMENT_SOURCE_ATTR

VERCEL_SOURCE_NAME = "vercel"
# dlt resource / staging-table names. cognee scopes orphan cleanup by table, so
# these are part of the contract: renaming one forgets and re-ingests its rows.
VERCEL_PROJECTS_TABLE = "vercel_projects"
VERCEL_DEPLOYMENTS_TABLE = "vercel_deployments"
VERCEL_BUILD_LOGS_TABLE = "vercel_build_logs"

_API_BASE = "https://api.vercel.com"
_PAGE_SIZE = 100
# Parent of the events call (listed, never loaded) and the per-line child rows.
_FAILED_DEPLOYMENTS = "vercel_failed_deployments"
_BUILD_EVENTS = "vercel_build_events"
# rest_api copies parent fields onto child rows as _<parent resource>_<field>.
_PARENT_UID = f"_{_FAILED_DEPLOYMENTS}_uid"
_PARENT_NAME = f"_{_FAILED_DEPLOYMENTS}_name"
_PARENT_URL = f"_{_FAILED_DEPLOYMENTS}_inspector_url"

_GIT_PROVIDERS = ("github", "gitlab", "bitbucket")
_TRUNCATED_MARKER = "[earlier output truncated]"

_EXTRA_HINT = "The Vercel connector requires dlt: pip install 'dlt[sqlalchemy]>=1.9.0,<2'."


def vercel_source(
    token: str | None = None,
    team_id: str | None = None,
    project_ids: list[str] | None = None,
    lookback_days: int | None = 30,
    include_build_logs: bool = True,
    max_log_chars: int | None = 20000,
    session: Any = None,
):
    """Create a dlt source that yields Vercel projects, deployments and failed-build output.

    Args:
        token: Vercel access token, sent as a bearer token. Falls back to
            ``VERCEL_TOKEN``.
        team_id: Team to read. Needed when a full-account token should read a team.
            Falls back to ``VERCEL_TEAM_ID``.
        project_ids: Restrict ingestion to these project ids or names. When omitted,
            every project the token can see is ingested.
        lookback_days: Deployments created within this many days stay in memory and
            older ones are forgotten on the next sync. ``None`` reads every deployment.
        include_build_logs: Fetch build output for deployments in state ``ERROR``.
        max_log_chars: Keep only the last N characters of a build log, where the
            error usually is. ``None`` keeps the whole log.
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

    resolved_token = token or os.environ.get("VERCEL_TOKEN")
    if not resolved_token:
        raise ValueError("Vercel access token required: pass token= or set VERCEL_TOKEN.")
    resolved_team = team_id or os.environ.get("VERCEL_TEAM_ID")
    wanted = set(project_ids or [])

    # One fixed lower bound per run. The whole window is re-read every time, so a
    # deployment that changed state or was deleted since the last run is seen.
    window: dict[str, Any] = {}
    if lookback_days is not None:
        since = datetime.now(UTC) - timedelta(days=lookback_days)
        window["since"] = int(since.timestamp() * 1000)
    scope = {"teamId": resolved_team} if resolved_team else {}

    def listing(path: str, key: str, **params: Any) -> dict:
        return {
            "path": path,
            "params": {"limit": _PAGE_SIZE, **scope, **params},
            "data_selector": key,
            # Vercel pages backwards in time: pagination.next goes back as `until`.
            "paginator": {
                "type": "cursor",
                "cursor_path": "pagination.next",
                "cursor_param": "until",
            },
            "response_actions": [_require_list(key)],
        }

    def wanted_project(project: dict) -> bool:
        return not wanted or project.get("id") in wanted or project.get("name") in wanted

    def wanted_deployment(deployment: dict) -> bool:
        in_scope = (
            not wanted or deployment.get("projectId") in wanted or deployment.get("name") in wanted
        )
        return in_scope and _is_live(deployment)

    resources: list[dict] = [
        {
            "name": VERCEL_PROJECTS_TABLE,
            "primary_key": "id",
            "write_disposition": "replace",
            "endpoint": listing("v10/projects", "projects"),
            "processing_steps": [{"filter": wanted_project}, {"map": _project_row}],
        },
        {
            "name": VERCEL_DEPLOYMENTS_TABLE,
            "primary_key": "id",
            "write_disposition": "replace",
            "endpoint": listing("v7/deployments", "deployments", **window),
            "processing_steps": [{"filter": wanted_deployment}, {"map": _deployment_row}],
        },
    ]
    if include_build_logs:
        resources += [
            {
                # A second pass over the same list, filtered to failed deployments on
                # the server. It only drives the events call below and is not loaded,
                # so successful deployments never trigger an events request.
                "name": _FAILED_DEPLOYMENTS,
                "selected": False,
                "endpoint": listing("v7/deployments", "deployments", state="ERROR", **window),
                "processing_steps": [{"filter": wanted_deployment}, {"map": _failed_parent}],
            },
            {
                "name": _BUILD_EVENTS,
                "include_from_parent": ["uid", "name", "inspector_url"],
                "endpoint": {
                    "path": f"v3/deployments/{{resources.{_FAILED_DEPLOYMENTS}.uid}}/events",
                    # -1 returns every available log line in one response.
                    "params": {"limit": -1, **scope},
                    "data_selector": "$",
                    "paginator": "single_page",
                    # The deployment was deleted between the two calls: no log to
                    # keep. Any other error aborts the run.
                    "response_actions": [
                        {"status_code": 404, "action": "ignore"},
                        {"status_code": 410, "action": "ignore"},
                    ],
                },
            },
        ]

    client: dict[str, Any] = {
        "base_url": _API_BASE,
        "auth": {"type": "bearer", "token": resolved_token},
    }
    if session is not None:
        client["session"] = session

    by_name = {
        resource.name: resource
        for resource in rest_api_resources({"client": client, "resources": resources})
    }
    loaded = [by_name[VERCEL_PROJECTS_TABLE], by_name[VERCEL_DEPLOYMENTS_TABLE]]

    if include_build_logs:

        @dlt.transformer(
            name=VERCEL_BUILD_LOGS_TABLE, primary_key="id", write_disposition="replace"
        )
        def vercel_build_logs(events):
            # rest_api yields one page per events request, so `events` holds every
            # log line of one failed deployment.
            yield from _build_log_rows(events, max_log_chars)

        loaded.append(by_name[_BUILD_EVENTS] | vercel_build_logs)

    # max_table_nesting=0: rows are flat already, and this keeps dlt from ever
    # unpacking a nested value into a child table that cognee would read back.
    @dlt.source(name=VERCEL_SOURCE_NAME, max_table_nesting=0)
    def _vercel():
        return loaded

    source = _vercel()
    # Opt into the document ingestion path (row -> text document -> cognify).
    # Set on the object that is returned: resolve_dlt_sources reads it from there.
    setattr(source, DOCUMENT_SOURCE_ATTR, VERCEL_SOURCE_NAME)
    return source


# ---------------------------------------------------------------------------
# Response guard and row builders (module-private)
# ---------------------------------------------------------------------------


def _require_list(key: str):
    """Build a response hook that aborts unless the body holds a list under ``key``.

    The projects endpoint is documented with a bare-array response as well. With
    ``data_selector`` set, such a body would yield no rows and no error, and under
    replace an empty read forgets everything. Raising keeps memory untouched.
    """

    def check(response, *args, **kwargs):
        if not response.ok:
            return  # dlt raises for error statuses once the hooks have run
        body = response.json()
        if not isinstance(body, dict) or not isinstance(body.get(key), list):
            raise ValueError(
                f"Vercel: expected an object with a '{key}' list from {response.url}. "
                "Aborting so a partial snapshot cannot forget live rows."
            )

    return check


def _is_live(deployment: dict) -> bool:
    """False for a soft-deleted deployment, which the list can still return."""
    return deployment.get("state") != "DELETED"


def _project_row(project: dict) -> dict:
    """Build a project document row from named fields only.

    ``updatedAt`` is left out on purpose: it moves without any change to the
    fields below and would churn the content hash on every sync.
    """
    link = project.get("link") or {}
    repo_keys = ("org", "repo", "projectNamespace", "projectName", "owner", "slug")
    repo = "/".join(str(link[key]) for key in repo_keys if link.get(key))
    name = project.get("name") or project.get("id")
    lines = _lines(
        ("Framework", project.get("framework")),
        ("Git repository", f"{link.get('type')}: {repo}" if repo else None),
        ("Production branch", link.get("productionBranch")),
        ("Root directory", project.get("rootDirectory")),
        ("Node.js version", project.get("nodeVersion")),
        ("Created", _timestamp(project.get("createdAt"))),
    )
    return {
        "id": project.get("id"),
        "title": f"Vercel project {name}",
        "content": "\n".join(lines),
    }


def _deployment_row(deployment: dict) -> dict:
    """Build a deployment document row from named fields only.

    The whole ``meta`` map is never copied: only the commit keys are read. The
    creator's email is left out; the username identifies the author.
    """
    meta = deployment.get("meta") or {}
    creator = deployment.get("creator") or {}
    uid = deployment.get("uid")
    name = deployment.get("name") or deployment.get("projectId") or ""
    # Vercel reports preview deployments with a null target.
    target = deployment.get("target") or "preview"
    error = ": ".join(
        str(deployment[key]) for key in ("errorCode", "errorMessage") if deployment.get(key)
    )
    host = deployment.get("url")
    lines = _lines(
        ("Project", name),
        ("Target", target),
        ("State", deployment.get("state") or deployment.get("readyState")),
        ("Created", _timestamp(deployment.get("created"))),
        ("Build started", _timestamp(deployment.get("buildingAt"))),
        # Vercel sets `ready` on failed deployments too: it is when the build ended.
        ("Finished", _timestamp(deployment.get("ready"))),
        ("Created by", creator.get("username")),
        ("Commit", _commit_meta(meta, "CommitSha")),
        ("Branch", _commit_meta(meta, "CommitRef")),
        ("Commit message", _commit_meta(meta, "CommitMessage")),
        (
            "Commit author",
            _commit_meta(meta, "CommitAuthorLogin") or _commit_meta(meta, "CommitAuthorName"),
        ),
        ("Error", error),
        ("Deployment URL", f"https://{host}" if host else None),
    )
    return {
        "id": uid,
        "title": f"Vercel deployment {uid} of {name} ({target})",
        "content": "\n".join(lines),
        "url": deployment.get("inspectorUrl"),
    }


def _failed_parent(deployment: dict) -> dict:
    """Reduce a failed deployment to the three fields the events call needs."""
    return {
        "uid": deployment.get("uid"),
        "name": deployment.get("name") or "",
        "inspector_url": deployment.get("inspectorUrl") or "",
    }


def _build_log_rows(events: Any, max_log_chars: int | None):
    """Join the log lines of each failed deployment into one document row.

    A failed deployment with no log lines (for example a build that never
    started) yields no row here. Its deployment document still carries the error
    code and message.
    """
    items = events if isinstance(events, list) else [events]
    for uid, group in groupby(items, key=lambda event: event.get(_PARENT_UID)):
        lines = list(group)
        text = "\n".join(filter(None, (_event_text(event) for event in lines)))
        if not uid or not text:
            continue
        if max_log_chars is not None and len(text) > max_log_chars:
            text = f"{_TRUNCATED_MARKER}\n{text[-max_log_chars:]}"
        name = lines[0].get(_PARENT_NAME)
        yield {
            "id": uid,
            "title": f"Build output of failed Vercel deployment {uid} of {name}",
            "content": text,
            "url": lines[0].get(_PARENT_URL) or None,
        }


def _event_text(event: dict) -> str:
    """Return the log line of a build event, from either documented shape."""
    text = event.get("text")
    if not isinstance(text, str):
        payload = event.get("payload")
        text = payload.get("text") if isinstance(payload, dict) else None
    return text.rstrip("\n") if isinstance(text, str) else ""


def _commit_meta(meta: dict, suffix: str) -> str | None:
    """Read one commit field from ``meta``, whichever Git provider set it."""
    for provider in _GIT_PROVIDERS:
        value = meta.get(f"{provider}{suffix}")
        if value:
            return str(value)
    return None


def _timestamp(milliseconds: Any) -> str | None:
    """Render a Vercel millisecond timestamp as UTC text, the same way every time."""
    if not isinstance(milliseconds, (int, float)) or isinstance(milliseconds, bool):
        return None
    moment = datetime.fromtimestamp(milliseconds / 1000, tz=UTC)
    return moment.strftime("%Y-%m-%d %H:%M UTC")


def _lines(*pairs: tuple[str, Any]) -> list[str]:
    """Render ``label: value`` lines, skipping empty values."""
    return [f"{label}: {value}" for label, value in pairs if value not in (None, "")]
