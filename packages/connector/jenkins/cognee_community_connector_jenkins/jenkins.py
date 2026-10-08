"""Jenkins connector for cognee — a ``dlt`` source that turns CI history into memory.

Pull Jenkins jobs and their builds (result, timing, causes, and the console log
of failed builds) into cognee, incrementally and with forget-on-delete — "why
did the nightly fail last week?".  Like the Confluence and Gmail connectors this
builds entirely on cognee's existing DLT ingestion path; hand the resource to
:func:`cognee.remember`::

    import cognee
    from cognee_community_connector_jenkins import jenkins_source

    await cognee.remember(
        jenkins_source(
            base_url="https://jenkins.example.com",
            username="you",
            api_token="…",
            job_names=["backend/main", "nightly-e2e"],
        ),
        dataset_name="ci_history",
        primary_key="id",
        write_disposition="merge",   # incremental upsert by record id
        max_rows_per_table=0,        # 0 = no row cap (forget-on-delete sees everything)
    )

Design
------
* **Auth** — Jenkins username + API token, sent as HTTP Basic auth.  The
  connector only issues ``GET`` requests.
* **Records** — one table, two kinds of row, told apart by ``kind``:
  a ``job`` row (description, parameters, last build) with id
  ``job:<full name>`` and a ``build`` row (result, timestamp, duration,
  causes, and for failed builds the tail of the console log) with id
  ``build:<full name>#<number>``.
* **Bounded requests** — every JSON call names its fields with ``tree=``, so a
  large install never answers with the full object graph (``depth=`` on a big
  controller can return hundreds of megabytes).  Console logs are streamed and
  only the last ``max_log_bytes`` are kept.
* **Incremental cursor** — per job, the highest build number that has been
  fully ingested (``last_build`` in dlt's per-resource state).  Each run reads
  the job's current build numbers and fetches only builds above the cursor.
  A build that is still running is not ingested and holds the cursor back, so
  it is picked up once it completes.
* **Forget-on-delete** — Jenkins has no deletion feed, so each run sweeps the
  configured jobs and compares against the previous run.  A job that vanished
  is emitted as a hard delete together with all of its builds, and builds that
  Jenkins discarded (log rotation / "discard old builds", or deleted by hand)
  are hard-deleted too.  dlt removes those rows on ``merge`` and cognee's
  existing ``orphan_cleanup`` purges them from the graph and vector stores.
* **Fail closed** — a sweep that finds no jobs while jobs were known is treated
  as a broken listing (wrong folder, permissions, network), not as "everything
  was deleted": nothing is deleted and the state is kept.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections import deque
from collections.abc import Iterable, Iterator
from typing import Any
from urllib.parse import quote, urlsplit

from cognee.shared.logging_utils import get_logger

logger = get_logger("jenkins_connector")

JENKINS_TABLE_NAME = "jenkins_records"

# Build results whose console log is fetched by default.
DEFAULT_LOG_RESULTS = ("FAILURE",)
# Keep the last 64 KiB of a failed build's log: the error is almost always near the end.
DEFAULT_MAX_LOG_BYTES = 64 * 1024
# On the first sync of a job, ingest at most this many of its most recent builds.
DEFAULT_MAX_BUILDS_PER_JOB = 20
# How deep to descend into folders / multibranch projects when no job list is given.
DEFAULT_MAX_FOLDER_DEPTH = 5

_JOB_TREE = (
    "name,fullName,description,buildable,color,"
    "property[parameterDefinitions[name,type,description]],"
    "lastBuild[number],allBuilds[number]"
)
_BUILD_TREE = (
    "number,result,building,timestamp,duration,displayName,description,"
    "actions[causes[shortDescription]]"
)
_LIST_TREE = "jobs[name,fullName,_class,jobs[name]{0,1}]"

# Values that look like credentials in log lines (Jenkins already masks bound
# credentials; this catches secrets printed by build scripts themselves). The
# keyword may sit inside an identifier such as DEPLOY_TOKEN or AWS_SECRET_ACCESS_KEY.
_SECRET_RE = re.compile(
    r"(?i)\b((?:[a-z0-9]+[_-])*"
    r"(?:password|passwd|pwd|secret|token|api[_-]?key|access[_-]?key)"
    r"(?:[_-][a-z0-9]+)*)(\s*[=:]\s*)(\S+)"
)
_REDACTED = "****"


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------
def _make_session(username: str, api_token: str) -> Any:
    """Build a ``requests`` session with Basic auth and retries on transient errors."""
    try:
        import requests
        from requests.adapters import HTTPAdapter
        from urllib3.util.retry import Retry
    except ImportError as exc:  # pragma: no cover - depends on the environment
        raise ImportError(
            'The Jenkins connector requires "requests": pip install requests'
        ) from exc

    session = requests.Session()
    session.auth = (username, api_token)
    session.headers.update({"Accept": "application/json"})
    retry = Retry(
        total=4,
        backoff_factor=1.0,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=("GET",),
        respect_retry_after_header=True,
    )
    adapter = HTTPAdapter(max_retries=retry)
    session.mount("http://", adapter)
    session.mount("https://", adapter)
    return session


class _NotFoundError(Exception):
    """The requested Jenkins object does not exist (HTTP 404)."""


def _get_json(session: Any, url: str, tree: str) -> dict:
    response = session.get(url, params={"tree": tree}, timeout=30)
    if response.status_code == 404:
        raise _NotFoundError(url)
    response.raise_for_status()
    return response.json()


def _job_path(full_name: str) -> str:
    """Map ``folder/sub/job`` to ``/job/folder/job/sub/job/job/`` with each segment encoded."""
    segments = [s for s in full_name.split("/") if s]
    if not segments:
        raise ValueError(f"invalid Jenkins job name: {full_name!r}")
    return "".join(f"/job/{quote(s, safe='')}" for s in segments) + "/"


def _validate_base_url(base_url: str) -> str:
    parts = urlsplit(base_url)
    if parts.scheme not in ("http", "https") or not parts.netloc:
        raise ValueError(f"base_url must be an http(s) URL, got {base_url!r}")
    if parts.username or parts.password:
        raise ValueError("Do not put credentials in base_url; pass username= and api_token=.")
    return base_url.rstrip("/")


# ---------------------------------------------------------------------------
# Jenkins reads
# ---------------------------------------------------------------------------
def _discover_jobs(session: Any, base_url: str, max_depth: int) -> list[str]:
    """Return the full names of every buildable job, descending into folders."""
    found: list[str] = []

    def walk(path: str, depth: int) -> None:
        data = _get_json(session, f"{base_url}{path}api/json", _LIST_TREE)
        for item in data.get("jobs") or []:
            full_name = item.get("fullName") or item.get("name")
            if not full_name:
                continue
            if "jobs" in item:  # a folder, multibranch project or organization folder
                if depth < max_depth:
                    walk(_job_path(full_name), depth + 1)
                else:
                    logger.warning(
                        "Jenkins: not descending into %s (max_folder_depth=%d).",
                        full_name,
                        max_depth,
                    )
            else:
                found.append(full_name)

    walk("/", 0)
    return sorted(set(found))


def _read_console_tail(session: Any, url: str, max_bytes: int) -> tuple[str, bool]:
    """Stream a console log and keep only its last ``max_bytes``; report truncation."""
    response = session.get(url, stream=True, timeout=60)
    try:
        if response.status_code == 404:
            return "", False
        response.raise_for_status()
        tail: deque[bytes] = deque()
        kept = 0
        total = 0
        for chunk in response.iter_content(chunk_size=8192):
            if not chunk:
                continue
            total += len(chunk)
            tail.append(chunk)
            kept += len(chunk)
            while tail and kept - len(tail[0]) >= max_bytes:
                kept -= len(tail.popleft())
        data = b"".join(tail)[-max_bytes:] if max_bytes > 0 else b""
        truncated = total > len(data)
        return data.decode("utf-8", errors="replace"), truncated
    finally:
        response.close()


def redact(text: str) -> str:
    """Mask values that look like credentials (``token=abc`` → ``token=****``)."""
    return _SECRET_RE.sub(lambda m: f"{m.group(1)}{m.group(2)}{_REDACTED}", text)


# ---------------------------------------------------------------------------
# Rows
# ---------------------------------------------------------------------------
def job_id(full_name: str) -> str:
    return f"job:{full_name}"


def build_id(full_name: str, number: int) -> str:
    return f"build:{full_name}#{number}"


def _deleted_row(record_id: str) -> dict[str, Any]:
    return {"id": record_id, "_deleted": True}


def _parameters(job: dict) -> list[dict[str, str]]:
    """Parameter names, types and descriptions only — default values may hold secrets."""
    params: list[dict[str, str]] = []
    for prop in job.get("property") or []:
        for definition in (prop or {}).get("parameterDefinitions") or []:
            params.append(
                {
                    "name": definition.get("name") or "",
                    "type": definition.get("type") or "",
                    "description": definition.get("description") or "",
                }
            )
    return params


def _job_row(base_url: str, full_name: str, job: dict) -> dict[str, Any]:
    params = _parameters(job)
    last = (job.get("lastBuild") or {}).get("number")
    lines = [f"Jenkins job {full_name}"]
    if job.get("description"):
        lines.append(f"Description: {job['description']}")
    if params:
        lines.append(
            "Parameters: " + ", ".join(f"{p['name']} ({p['type']})" for p in params if p["name"])
        )
    if job.get("buildable") is False:
        lines.append("The job is disabled.")
    return {
        "id": job_id(full_name),
        "kind": "job",
        "job": full_name,
        "url": f"{base_url}{_job_path(full_name)}",
        "description": job.get("description") or "",
        "buildable": job.get("buildable") is not False,
        "parameters": json.dumps(params, sort_keys=True),
        "last_build": last,
        "content": "\n".join(lines),
        "_deleted": False,
    }


def _build_row(
    base_url: str, full_name: str, build: dict, log: str | None, truncated: bool
) -> dict[str, Any]:
    number = build.get("number")
    causes = sorted(
        {
            cause.get("shortDescription")
            for action in build.get("actions") or []
            for cause in (action or {}).get("causes") or []
            if cause.get("shortDescription")
        }
    )
    result = build.get("result") or "UNKNOWN"
    lines = [f"Jenkins build {full_name} #{number}: {result}"]
    if causes:
        lines.append("Cause: " + "; ".join(causes))
    if build.get("description"):
        lines.append(f"Description: {build['description']}")
    if log:
        header = "Console log (tail)" if truncated else "Console log"
        lines.append(f"{header}:\n{log}")
    return {
        "id": build_id(full_name, number),
        "kind": "build",
        "job": full_name,
        "number": number,
        "url": f"{base_url}{_job_path(full_name)}{number}/",
        "result": result,
        "timestamp": build.get("timestamp"),
        "duration_ms": build.get("duration"),
        "causes": json.dumps(causes),
        "log_truncated": truncated,
        "content": "\n".join(lines),
        "_deleted": False,
    }


def _fingerprint(row: dict[str, Any]) -> str:
    """Stable hash of a job row's meaningful fields (so unchanged jobs are not re-emitted)."""
    fields = {k: row[k] for k in ("description", "buildable", "parameters", "last_build")}
    return hashlib.sha256(json.dumps(fields, sort_keys=True).encode()).hexdigest()[:16]


# ---------------------------------------------------------------------------
# Sync (pure given a session + state dict — unit-testable)
# ---------------------------------------------------------------------------
def sync_jenkins(
    session: Any,
    base_url: str,
    state: dict,
    *,
    job_names: Iterable[str] | None = None,
    max_builds_per_job: int = DEFAULT_MAX_BUILDS_PER_JOB,
    log_results: Iterable[str] = DEFAULT_LOG_RESULTS,
    max_log_bytes: int = DEFAULT_MAX_LOG_BYTES,
    max_folder_depth: int = DEFAULT_MAX_FOLDER_DEPTH,
    redact_logs: bool = True,
) -> Iterator[dict[str, Any]]:
    """Yield changed jobs and new builds since the last run, plus hard-delete markers.

    ``state`` maps ``"jobs"`` to ``{full name: {"last_build", "builds", "fingerprint"}}``
    and is updated in place; dlt persists it only when the load succeeds, so a
    failed run is retried from the previous state.
    """
    log_results = {r.upper() for r in log_results}
    known: dict[str, dict] = state.get("jobs") or {}

    if job_names:
        targets = sorted({name.strip("/") for name in job_names if name.strip("/")})
    else:
        targets = _discover_jobs(session, base_url, max_folder_depth)

    current: dict[str, dict] = {}
    emitted = deleted = 0

    for full_name in targets:
        try:
            job = _get_json(session, f"{base_url}{_job_path(full_name)}api/json", _JOB_TREE)
        except _NotFoundError:
            # An explicitly listed job that no longer exists: forget it below.
            logger.warning("Jenkins: job %s not found; it will be forgotten.", full_name)
            continue

        prev = known.get(full_name) or {}
        cursor = int(prev.get("last_build") or 0)
        ingested = {int(n) for n in prev.get("builds") or []}
        existing = sorted(
            {int(b["number"]) for b in job.get("allBuilds") or [] if b.get("number") is not None}
        )

        job_row = _job_row(base_url, full_name, job)
        fingerprint = _fingerprint(job_row)
        if fingerprint != prev.get("fingerprint"):
            yield job_row
            emitted += 1

        # Builds Jenkins no longer has (rotated out or deleted) are forgotten.
        for number in sorted(ingested - set(existing)):
            yield _deleted_row(build_id(full_name, number))
            deleted += 1
        ingested &= set(existing)

        candidates = [n for n in existing if n > cursor]
        if not prev and max_builds_per_job > 0 and len(candidates) > max_builds_per_job:
            # First sync: only the most recent builds. Older ones stay below the
            # cursor on purpose, so later runs never backfill them either.
            candidates = candidates[-max_builds_per_job:]
            cursor = candidates[0] - 1

        new_cursor = cursor
        blocked = False  # a running build holds the cursor so it is fetched again later
        for number in candidates:
            if number in ingested:  # ingested earlier; finished builds do not change
                if not blocked:
                    new_cursor = number
                continue
            try:
                build = _get_json(
                    session, f"{base_url}{_job_path(full_name)}{number}/api/json", _BUILD_TREE
                )
            except _NotFoundError:  # deleted between the listing and now
                if not blocked:
                    new_cursor = number
                continue
            if build.get("building"):
                blocked = True
                continue

            log, truncated = None, False
            if (build.get("result") or "").upper() in log_results:
                log, truncated = _read_console_tail(
                    session,
                    f"{base_url}{_job_path(full_name)}{number}/consoleText",
                    max_log_bytes,
                )
                if redact_logs:
                    log = redact(log)
            yield _build_row(base_url, full_name, build, log, truncated)
            emitted += 1
            ingested.add(number)
            if not blocked:
                new_cursor = number

        current[full_name] = {
            "last_build": new_cursor,
            "builds": sorted(ingested),
            "fingerprint": fingerprint,
        }

    if known and not current:
        logger.warning(
            "Jenkins: the sweep found 0 jobs but %d were known; skipping deletion this run "
            "to avoid forgetting everything on a broken listing.",
            len(known),
        )
        logger.info("Jenkins: %d record(s) emitted, 0 deletion(s).", emitted)
        return

    for full_name in sorted(set(known) - set(current)):
        yield _deleted_row(job_id(full_name))
        deleted += 1
        for number in known[full_name].get("builds") or []:
            yield _deleted_row(build_id(full_name, int(number)))
            deleted += 1

    state["jobs"] = current
    logger.info("Jenkins: %d record(s) emitted, %d deletion(s).", emitted, deleted)


# ---------------------------------------------------------------------------
# Public factory
# ---------------------------------------------------------------------------
def jenkins_source(
    *,
    base_url: str,
    username: str | None = None,
    api_token: str | None = None,
    job_names: list[str] | None = None,
    max_builds_per_job: int = DEFAULT_MAX_BUILDS_PER_JOB,
    log_results: Iterable[str] = DEFAULT_LOG_RESULTS,
    max_log_bytes: int = DEFAULT_MAX_LOG_BYTES,
    max_folder_depth: int = DEFAULT_MAX_FOLDER_DEPTH,
    redact_logs: bool = True,
    session: Any = None,
):
    """Return a ``dlt`` resource that yields Jenkins jobs and builds for ``remember``.

    Args:
        base_url: Jenkins root URL, e.g. ``https://jenkins.example.com`` (may include
            a context path such as ``https://ci.example.com/jenkins``).
        username: Jenkins user (Basic-auth username).
        api_token: That user's API token (Basic-auth password).
        job_names: Full job names to sync (``"folder/job"`` for jobs in folders).
            ``None`` syncs every job the user can see, descending into folders.
        max_builds_per_job: On a job's first sync, ingest only this many of its most
            recent builds (``0`` = all).  Later runs fetch every new build.
        log_results: Build results whose console log is ingested (default: failures).
        max_log_bytes: Keep only the last N bytes of each ingested console log.
        max_folder_depth: How deep job discovery descends into folders.
        redact_logs: Mask values that look like credentials in ingested logs.
        session: Pre-built ``requests`` session (mainly a test injection point).

    Returns:
        A ``dlt`` resource (``jenkins_records``) with ``primary_key="id"``,
        ``write_disposition="merge"`` and an ``_deleted`` hard-delete column.
    """
    try:
        import dlt
    except ImportError as exc:
        raise ImportError(
            'The Jenkins connector requires dlt: pip install "dlt[sqlalchemy]".'
        ) from exc

    base_url = _validate_base_url(base_url)
    if session is None and not (username and api_token):
        raise ValueError("jenkins_source requires username and api_token (or an injected session).")

    @dlt.resource(
        name=JENKINS_TABLE_NAME,
        primary_key="id",
        write_disposition="merge",
        columns={"_deleted": {"data_type": "bool", "hard_delete": True}},
    )
    def jenkins_records():
        client = session or _make_session(username, api_token)
        yield from sync_jenkins(
            client,
            base_url,
            dlt.current.resource_state(),
            job_names=job_names,
            max_builds_per_job=max_builds_per_job,
            log_results=log_results,
            max_log_bytes=max_log_bytes,
            max_folder_depth=max_folder_depth,
            redact_logs=redact_logs,
        )

    return jenkins_records
