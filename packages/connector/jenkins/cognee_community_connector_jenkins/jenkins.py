"""Incremental Jenkins documents for Cognee's scoped DLT ingestion path.

Build-number requests use pages of at most 100 entries. Complete discovery and
build inventories authorize deletion; incomplete listings fail closed. An optional
confirm_deleted callback can veto deletion. Credential masking is best effort.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections import deque
from collections.abc import Callable, Iterable, Iterator
from typing import Any
from urllib.parse import quote, urlsplit

from cognee.shared.logging_utils import get_logger
from cognee.tasks.ingestion import dlt_utils

DOCUMENT_SOURCE_ATTR = getattr(dlt_utils, "DOCUMENT_SOURCE_ATTR", None)
PIPELINE_SCOPE_ATTR = getattr(dlt_utils, "PIPELINE_SCOPE_ATTR", None)


def _require_document_sync() -> None:
    version = getattr(dlt_utils, "DOCUMENT_SYNC_VERSION", None)
    if (
        type(version) is not int
        or version < 2
        or not isinstance(DOCUMENT_SOURCE_ATTR, str)
        or not DOCUMENT_SOURCE_ATTR
        or not isinstance(PIPELINE_SCOPE_ATTR, str)
        or not PIPELINE_SCOPE_ATTR
    ):
        raise RuntimeError(
            "Jenkins requires Cognee scoped document sync version >=2 and pipeline scope "
            "support (released in cognee 1.6.5); refusing unsafe ingestion."
        )


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
    "name,fullName,description,buildable,"
    "property[parameterDefinitions[name,type,description]],"
    "lastBuild[number],"
)
_BUILD_TREE = "number,result,building,description,actions[causes[shortDescription]]"
_LIST_TREE = "jobs[name,fullName,jobs[name]{0,1}]"

# Best-effort masking of common credential formats. Do not assume this is a
# comprehensive DLP boundary: only ingest logs from a trusted Jenkins scope.
_SECRET_RE = re.compile(
    r"(?i)\b((?:[a-z0-9]+[_-])*"
    r"(?:password|passwd|pwd|secret|token|api[_-]?key|access[_-]?key)"
    r"(?:[_-][a-z0-9]+)*)([\"\']?\s*[=:]\s*[\"\']?)([^\"\'\s,;}]+)"
)
_BEARER_RE = re.compile(r"(?i)(\bAuthorization\s*:\s*Bearer\s+)([^\s\"\',;]+)")
_CURL_USER_RE = re.compile(r"(?i)(\bcurl\b[^\n]*?\s-u\s+)([^\s]+)")
_GITHUB_PAT_RE = re.compile(r"\b(?:ghp_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})\b")
_REDACTED = "****"


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------
def _make_session(username: str, api_token: str, check_active=None) -> Any:
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

    class CappedRetry(Retry):
        def sleep(self, response=None):
            if check_active:
                check_active()
            super().sleep(response)
            if check_active:
                check_active()

        def get_retry_after(self, response):
            try:
                delay = super().get_retry_after(response)
            except Exception:
                # A malformed server header must not expose response details.
                return None
            return min(delay, 30) if delay is not None else None

    retry = CappedRetry(
        total=4,
        backoff_factor=1.0,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=("GET",),
        respect_retry_after_header=True,
        raise_on_status=False,
    )
    adapter = HTTPAdapter(max_retries=retry)
    session.mount("http://", adapter)
    session.mount("https://", adapter)
    return session


class _NotFoundError(Exception):
    """The requested Jenkins object does not exist (HTTP 404)."""


def _close_http_object(value: Any) -> None:
    try:
        value.close()
    except Exception:
        raise RuntimeError("Jenkins HTTP cleanup failed") from None


def _get_json(session: Any, url: str, tree: str) -> dict:
    response = None
    try:
        response = session.get(url, params={"tree": tree}, timeout=30)
        if response.status_code == 404:
            raise _NotFoundError()
        if response.status_code == 429:
            raise _RateLimitError("Jenkins HTTP 429")
        response.raise_for_status()
        if response.status_code != 200:
            raise RuntimeError("Jenkins JSON listing requires HTTP 200")
        result = response.json()
        if not isinstance(result, dict):
            raise RuntimeError("Jenkins returned invalid JSON")
        return result
    except (_NotFoundError, _RateLimitError):
        raise
    except Exception:
        raise RuntimeError("Jenkins JSON request failed") from None
    finally:
        if response is not None:
            _close_http_object(response)


class _RateLimitError(RuntimeError):
    """Rate limiting remained after bounded retries."""


def _partial_listing(data: dict, key: str) -> bool:
    return (
        any(data.get(flag) for flag in ("truncated", "hasMore", "has_more", "partial", "next"))
        or data.get("complete") is False
        or ("total" in data and data["total"] != len(data.get(key) or []))
    )


def _job_path(full_name: str) -> str:
    """Map ``folder/sub/job`` to ``/job/folder/job/sub/job/job/`` with each segment encoded."""
    segments = [s for s in full_name.split("/") if s]
    if not segments:
        raise ValueError("invalid Jenkins job name")
    return "".join(f"/job/{quote(s, safe='')}" for s in segments) + "/"


def _validate_base_url(base_url: str) -> str:
    try:
        parts = urlsplit(base_url)
        port = parts.port
    except (ValueError, TypeError):
        raise ValueError("base_url must be a valid http(s) URL") from None
    if parts.scheme not in ("http", "https") or not parts.netloc:
        raise ValueError("base_url must be an http(s) URL")
    if parts.username or parts.password:
        raise ValueError("Do not put credentials in base_url; pass username= and api_token=.")
    if parts.query or parts.fragment or port == 0:
        raise ValueError("base_url must not include query parameters or fragments")
    return base_url.rstrip("/")


# ---------------------------------------------------------------------------
# Jenkins reads
# ---------------------------------------------------------------------------
def _discover_jobs(
    session: Any,
    base_url: str,
    max_depth: int,
    check_active: Callable[[], None] | None = None,
) -> list[str]:
    """Discover jobs only after every folder has returned a complete HTTP 200 list."""
    found: list[str] = []

    def walk(path: str, depth: int, prefix: str = "") -> None:
        if check_active:
            check_active()
        data = _get_json(session, f"{base_url}{path}api/json", _LIST_TREE)
        if _partial_listing(data, "jobs"):
            raise RuntimeError("Jenkins discovery incomplete: partial listing")
        if not isinstance(data.get("jobs"), list):
            raise RuntimeError("Jenkins job discovery was incomplete: missing jobs list")
        for item in data["jobs"]:
            if not isinstance(item, dict):
                raise RuntimeError("Jenkins discovery incomplete: malformed record")
            name = item.get("name")
            full_name = item.get("fullName")
            if not full_name:
                if not isinstance(name, str) or not name or "/" in name:
                    raise RuntimeError("Jenkins discovery incomplete: malformed name")
                full_name = f"{prefix}/{name}" if prefix else name
            if (
                not isinstance(full_name, str)
                or not full_name.strip("/")
                or (prefix and not full_name.startswith(prefix + "/"))
            ):
                raise RuntimeError("Jenkins discovery incomplete: malformed name")
            if "jobs" in item and not isinstance(item["jobs"], list):
                raise RuntimeError("Jenkins discovery incomplete: malformed folder")
            if "jobs" in item:  # a folder, multibranch project or organization folder
                if depth < max_depth:
                    walk(_job_path(full_name), depth + 1, full_name)
                else:
                    raise RuntimeError("Jenkins discovery incomplete: max_folder_depth reached")
            else:
                found.append(full_name)

    walk("/", 0)
    if len(found) != len(set(found)):
        raise RuntimeError("Jenkins discovery incomplete: duplicate jobs")
    return sorted(found)


def _read_console_tail(
    session: Any, url: str, max_bytes: int, check_active=None
) -> tuple[str, bool]:
    """Stream a console log and keep only its last ``max_bytes``; report truncation."""
    response = None
    try:
        if check_active:
            check_active()
        response = session.get(url, stream=True, timeout=60)
        if response.status_code == 404:
            return "", False
        if response.status_code == 429:
            raise _RateLimitError("Jenkins HTTP 429")
        response.raise_for_status()
        tail: deque[bytes] = deque()
        kept = 0
        total = 0
        for chunk in response.iter_content(chunk_size=8192):
            if check_active:
                check_active()
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
    except _RateLimitError:
        raise
    except Exception:
        raise RuntimeError("Jenkins console request failed") from None
    finally:
        if response is not None:
            _close_http_object(response)


def redact(text: str) -> str:
    """Best-effort mask of common credential-shaped values (not general DLP)."""
    text = _BEARER_RE.sub(lambda m: f"{m.group(1)}{_REDACTED}", text)
    text = _CURL_USER_RE.sub(lambda m: f"{m.group(1)}{_REDACTED}", text)
    text = _SECRET_RE.sub(lambda m: f"{m.group(1)}{m.group(2)}{_REDACTED}", text)
    return _GITHUB_PAT_RE.sub(_REDACTED, text)


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
    lines = [f"Jenkins job {full_name}"]
    if job.get("description"):
        lines.append(f"Description: {job['description']}")
    if params:
        lines.append(
            "Parameters: "
            + ", ".join(
                f"{p['name']} ({p['type']}): {p['description']}" for p in params if p["name"]
            )
        )
    if job.get("buildable") is False:
        lines.append("The job is disabled.")
    return {
        "id": job_id(full_name),
        "title": f"Jenkins job {full_name}",
        "content": redact("\n".join(lines)),
        "url": f"{base_url}{_job_path(full_name)}",
        "_deleted": False,
    }


def _build_row(
    base_url: str, full_name: str, build: dict, log: str | None, truncated: bool
) -> dict[str, Any]:
    number = build.get("number")
    causes = sorted(
        {
            redact(cause.get("shortDescription"))
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
        lines.append(f"Description: {redact(build['description'])}")
    if log:
        header = "Console log (tail)" if truncated else "Console log"
        lines.append(f"{header}:\n{log}")
    return {
        "id": build_id(full_name, number),
        "title": f"Jenkins build {full_name} #{number}: {result}",
        "content": "\n".join(lines),
        "url": f"{base_url}{_job_path(full_name)}{number}/",
        "_deleted": False,
    }


def _fingerprint(row: dict[str, Any]) -> str:
    """Hash document fields only; changing lastBuild must not re-extract a job."""
    fields = {k: row[k] for k in ("title", "content", "url")}
    return hashlib.sha256(json.dumps(fields, sort_keys=True).encode()).hexdigest()[:16]


# ---------------------------------------------------------------------------
# Sync (pure given a session + state dict — unit-testable)
# ---------------------------------------------------------------------------
def _sync_jenkins(
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
    check_active: Callable[[], None] | None = None,
    confirm_deleted: Callable[[str, int | None], bool] | None = None,
    stats: dict | None = None,
) -> Iterator[dict[str, Any]]:
    """Yield changed jobs and new builds since the last run, plus hard-delete markers.

    ``state`` maps ``"jobs"`` to ``{full name: {"last_build", "builds", "fingerprint"}}``
    and is updated in place; dlt persists it only when the load succeeds, so a
    failed run is retried from the previous state.
    """
    if max_builds_per_job < 0 or max_log_bytes < 0 or max_folder_depth < 0:
        raise ValueError("Jenkins limits must be non-negative")
    log_results = {r.upper() for r in log_results}

    def active():
        if check_active:
            check_active()

    active()
    known: dict[str, dict] = state.get("jobs") or {}

    if job_names is not None:
        targets = sorted({name.strip("/") for name in job_names if name.strip("/")})
    else:
        targets = _discover_jobs(session, base_url, max_folder_depth, check_active)

    current: dict[str, dict] = {}
    emitted = deleted = 0

    for full_name in targets:
        active()
        try:
            job = _get_json(
                session,
                f"{base_url}{_job_path(full_name)}api/json",
                _JOB_TREE + "allBuilds[number]{0,100}",
            )
        except _NotFoundError:
            # A 404 may mean an access-control change rather than deletion.
            # Listing omissions also need independent deletion evidence.
            raise RuntimeError("Jenkins job returned HTTP 404; refusing possible deletes") from None

        prev = known.get(full_name) or {}
        cursor = int(prev.get("last_build") or 0)
        ingested = {int(n) for n in prev.get("builds") or []}
        existing, builds_complete = _build_numbers(
            session,
            base_url,
            full_name,
            job,
            cursor,
            max_builds_per_job if not prev else 0,
            ingested,
            active,
        )
        stats["scanned"] += 1

        job_row = _job_row(base_url, full_name, job)
        fingerprint = _fingerprint(job_row)
        if fingerprint != prev.get("fingerprint"):
            active()
            yield job_row
            emitted += 1
        else:
            stats["skipped"] += 1

        # Only an exhausted allBuilds inventory authorizes rotation deletes.
        # The optional callback adds a veto; it cannot override incomplete data.
        for number in sorted(ingested - set(existing)):
            active()
            if builds_complete and (
                confirm_deleted is None or confirm_deleted(full_name, number) is True
            ):
                yield _deleted_row(build_id(full_name, number))
                deleted += 1
                ingested.remove(number)

        candidates = [n for n in existing if n > cursor]
        if not prev and max_builds_per_job > 0 and len(candidates) > max_builds_per_job:
            # First sync: only the most recent builds. Older ones stay below the
            # cursor on purpose, so later runs never backfill them either.
            candidates = candidates[-max_builds_per_job:]
            cursor = candidates[0] - 1

        new_cursor = cursor
        blocked = False  # a running build holds the cursor so it is fetched again later
        for number in candidates:
            active()
            stats["scanned"] += 1
            if number in ingested:
                stats["skipped"] += 1  # ingested earlier; finished builds do not change
                if not blocked:
                    new_cursor = number
                continue
            try:
                build = _get_json(
                    session, f"{base_url}{_job_path(full_name)}{number}/api/json", _BUILD_TREE
                )
            except _NotFoundError:
                # Could be a permission change: retry without advancing state.
                raise RuntimeError(
                    "Jenkins build returned HTTP 404; refusing cursor advance"
                ) from None
            if build.get("number") != number:
                raise RuntimeError("Jenkins returned a mismatched build number")
            if build.get("building"):
                stats["skipped"] += 1
                blocked = True
                continue

            log, truncated = None, False
            if (build.get("result") or "").upper() in log_results:
                log, truncated = _read_console_tail(
                    session,
                    f"{base_url}{_job_path(full_name)}{number}/consoleText",
                    max_log_bytes,
                    check_active,
                )
                if redact_logs:
                    log = redact(log)
            active()
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

    for full_name in sorted(set(known) - set(current)):
        active()
        if job_names is not None or (
            confirm_deleted is not None and confirm_deleted(full_name, None) is not True
        ):
            current[full_name] = known[full_name]
            continue
        yield _deleted_row(job_id(full_name))
        deleted += 1
        for number in known[full_name].get("builds") or []:
            active()
            yield _deleted_row(build_id(full_name, int(number)))
            deleted += 1

    active()
    state["jobs"] = current
    stats["emitted"] = emitted
    stats["deleted"] = deleted
    logger.info("Jenkins: %d record(s) emitted, %d deletion(s).", emitted, deleted)


def _build_numbers(session, base_url, full_name, first, cursor, initial_limit, retained, active):
    """Return build numbers and whether the bounded inventory was exhausted.

    Continue past short pages and the cursor when retained builds are missing.
    Early stops and missing allBuilds never authorize deletion. Duplicate or
    out-of-order pages abort without state commit.
    """
    numbers = set()
    data = first
    start = 0
    previous = None
    while True:
        if _partial_listing(data, "allBuilds"):
            raise RuntimeError("Jenkins build listing incomplete")
        if "allBuilds" not in data:
            last = (first.get("lastBuild") or {}).get("number")
            if start:
                raise RuntimeError("Jenkins build listing missing page")
            if last is not None:
                if type(last) is not int or last <= 0:
                    raise RuntimeError("Jenkins build listing malformed lastBuild")
                numbers.add(last)
            return sorted(numbers), False
        page = data["allBuilds"]
        if not isinstance(page, list) or len(page) > 100:
            raise RuntimeError("Jenkins build listing malformed or unbounded")
        if not page:
            return sorted(numbers), True
        for item in page:
            number = item.get("number") if isinstance(item, dict) else None
            if (
                type(number) is not int
                or number <= 0
                or (previous is not None and number >= previous)
            ):
                raise RuntimeError("Jenkins build listing malformed or unstable")
            previous = number
            numbers.add(number)
        if (min(numbers) <= cursor and retained <= numbers) or (
            initial_limit and len(numbers) >= initial_limit
        ):
            return sorted(numbers), False
        start += len(page)
        active()
        data = _get_json(
            session,
            f"{base_url}{_job_path(full_name)}api/json",
            f"allBuilds[number]{{{start},{start + 100}}}",
        )


def sync_jenkins(
    session: Any, base_url: str, state: dict, *, stats: dict | None = None, **kwargs
) -> Iterator[dict[str, Any]]:
    """Sync with per-run counters; state changes only after successful exhaustion.

    scanned counts evaluated jobs and candidate builds; skipped counts unchanged
    jobs, running or already ingested candidates. failed counts aborted runs and
    failed_rate_limit counts those aborted on a final HTTP 429. Counters describe
    source extraction, not a committed DLT/Cognee transaction.
    """
    stats = stats if stats is not None else {}
    stats.clear()
    stats.update(
        dict.fromkeys(
            ("scanned", "skipped", "failed", "deleted", "failed_rate_limit", "emitted"), 0
        )
    )
    try:
        for row in _sync_jenkins(session, base_url, state, stats=stats, **kwargs):
            stats["deleted" if row.get("_deleted") else "emitted"] += 1
            yield row
    except Exception as exc:
        stats["failed"] += 1
        if isinstance(exc, _RateLimitError):
            stats["failed_rate_limit"] += 1
        raise


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
    service: Any = None,
    check_active: Callable[[], None] | None = None,
    confirm_deleted: Callable[[str, int | None], bool] | None = None,
    resource_name: str = JENKINS_TABLE_NAME,
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
        service: Pre-built requests-compatible client; caller owns its lifetime.
        session: Backward-compatible alias for service; do not pass both.
        check_active: Synchronous callback before requests, retry waits, stream
            chunks, row emission and final state commit. Raise to cancel.
        confirm_deleted: Optional extra deletion guard: (full_name, build_number),
            or (full_name, None) for a whole job. When supplied, return exactly
            True to allow deletion. Missing/partial listings never authorize
            deletion, even with this callback. Default: delete on complete lists.
        resource_name: Stable unique DLT table/scope name for each Jenkins source.
            Use different names for different Jenkins installations.

    Returns:
        A ``dlt`` resource (``jenkins_records``) with ``primary_key="id"``,
        ``write_disposition="merge"`` and an ``_deleted`` hard-delete column.
    """
    _require_document_sync()
    if session is not None and service is not None:
        raise ValueError("Pass only one of service or session")
    session = service if service is not None else session
    try:
        import dlt
    except ImportError as exc:
        raise ImportError(
            'The Jenkins connector requires dlt: pip install "dlt[sqlalchemy]".'
        ) from exc

    base_url = _validate_base_url(base_url)
    if not resource_name or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", resource_name):
        raise ValueError("resource_name must be a non-empty DLT-safe identifier")
    if session is None and not (username and api_token):
        raise ValueError("jenkins_source requires username and api_token (or an injected session).")

    stats = {}

    @dlt.resource(
        name=resource_name,
        primary_key="id",
        write_disposition="merge",
        columns={"_deleted": {"data_type": "bool", "hard_delete": True}},
    )
    def jenkins_records():
        client = (
            session if session is not None else _make_session(username, api_token, check_active)
        )
        try:
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
                check_active=check_active,
                confirm_deleted=confirm_deleted,
                stats=stats,
            )
        finally:
            if session is None:
                _close_http_object(client)

    # Route repeated remember() calls through cognee document extraction rather
    # than treating the DLT schema as one completed relational manifest.
    jenkins_records.cognee_sync_stats = stats
    setattr(jenkins_records, DOCUMENT_SOURCE_ATTR, "jenkins")
    setattr(jenkins_records, PIPELINE_SCOPE_ATTR, resource_name)
    return jenkins_records
