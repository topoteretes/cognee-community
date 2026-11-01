"""DLT source for Jenkins job configuration and build outcomes."""

from __future__ import annotations

import hashlib
import json
import os
from typing import Any
from urllib.parse import urljoin, urlsplit

from cognee.shared.logging_utils import get_logger
from cognee.tasks.ingestion.dlt_utils import DOCUMENT_SOURCE_ATTR

logger = get_logger("jenkins_connector")

_SOURCE_NAME = "jenkins"
_RESOURCE_NAME = "jenkins_items"
_DEFAULT_INITIAL_BUILDS = 20
_MAX_CONSOLE_BYTES = 128 * 1024
_TREE_FIELDS = "name,fullName,url,_class,description,disabled,buildable,lastBuild[number]"
_JOB_LIST_TREE = f"jobs[{_TREE_FIELDS}]"


def jenkins_source(
    base_url: str | None = None,
    username: str | None = None,
    api_token: str | None = None,
    *,
    job_names: list[str] | None = None,
    include_job_config: bool = True,
    include_builds: bool = True,
    initial_builds: int = _DEFAULT_INITIAL_BUILDS,
    session: Any = None,
):
    """Create a DLT resource that yields Jenkins job and build documents.

    ``job_names`` selects jobs by their Jenkins ``fullName`` (for example
    ``team/service`` for a job inside a folder). When omitted, every visible job
    is selected. Credentials and the base URL can be supplied explicitly or via
    ``JENKINS_URL``, ``JENKINS_USER`` and ``JENKINS_API_TOKEN``.

    ``session`` is an optional preconfigured requests-compatible session used
    for testing. The default first sync includes up to ``initial_builds`` recent
    builds per job; subsequent syncs fetch every build number after the stored
    ``lastBuild`` watermark and revisit in-progress builds.
    """
    try:
        import dlt
    except ImportError as exc:
        raise ImportError(
            'The Jenkins connector requires the "dlt" extra: pip install "cognee[dlt]".'
        ) from exc

    resolved_url = (base_url or os.getenv("JENKINS_URL", "")).rstrip("/")
    resolved_username = username or os.getenv("JENKINS_USER")
    resolved_token = api_token or os.getenv("JENKINS_API_TOKEN")
    _validate_base_url(resolved_url)

    if session is None and not (resolved_username and resolved_token):
        raise ValueError("jenkins_source requires JENKINS_USER and JENKINS_API_TOKEN.")
    if job_names is not None and not job_names:
        raise ValueError("job_names must contain at least one Jenkins fullName.")
    if initial_builds < 1:
        raise ValueError("initial_builds must be at least 1.")

    @dlt.resource(
        name=_RESOURCE_NAME,
        primary_key="id",
        write_disposition="merge",
        columns={"_deleted": {"data_type": "bool", "hard_delete": True}},
    )
    def jenkins_items():
        client = session or _make_session(resolved_username, resolved_token)
        resource_state = dlt.current.resource_state()
        rows, next_state = _sync_jenkins(
            client,
            resolved_url,
            resource_state,
            job_names=job_names,
            include_job_config=include_job_config,
            include_builds=include_builds,
            initial_builds=initial_builds,
        )
        yield from rows
        # Advance checkpoints only after a complete successful inventory/build
        # pass. A failed request therefore cannot skip work on the next run.
        resource_state.clear()
        resource_state.update(next_state)

    resource = jenkins_items()
    setattr(resource, DOCUMENT_SOURCE_ATTR, _SOURCE_NAME)
    return resource


def _make_session(username: str, api_token: str) -> Any:
    """Build a read-only HTTP session using Jenkins' Basic user/token auth."""
    try:
        import requests
    except ImportError as exc:  # pragma: no cover - depends on optional dependency
        raise ImportError(
            "The Jenkins connector requires requests; install it with `uv sync` in this package."
        ) from exc

    client = requests.Session()
    client.auth = (username, api_token)
    client.headers.update({"Accept": "application/json"})
    return client


def _validate_base_url(base_url: str) -> None:
    parsed = urlsplit(base_url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("JENKINS_URL must be an absolute http:// or https:// URL.")
    if parsed.username or parsed.password:
        raise ValueError("JENKINS_URL must not contain embedded credentials.")


def _get_build_json(session: Any, url: str) -> dict[str, Any]:
    response = session.get(
        url,
        params={"tree": "number,result,timestamp,duration,building,url,displayName", "depth": 1},
        timeout=(5, 30),
    )
    response.raise_for_status()
    data = response.json()
    if not isinstance(data, dict):
        raise ValueError("Jenkins returned an invalid build JSON object.")
    return data


def _get_console_text(session: Any, url: str) -> str:
    """Read no more than ``_MAX_CONSOLE_BYTES`` from a failed build's console."""
    response = session.get(url, params={"start": 0}, timeout=(5, 30), stream=True)
    try:
        response.raise_for_status()
        chunks: list[bytes] = []
        size = 0
        for chunk in response.iter_content(chunk_size=8192):
            if not chunk:
                continue
            remaining = _MAX_CONSOLE_BYTES - size
            chunks.append(chunk[:remaining])
            size += min(len(chunk), remaining)
            if size >= _MAX_CONSOLE_BYTES:
                break
    finally:
        response.close()
    text = b"".join(chunks).decode("utf-8", errors="replace")
    if size >= _MAX_CONSOLE_BYTES:
        return f"{text}\n[Console output truncated at {_MAX_CONSOLE_BYTES} bytes.]"
    return text


def _sync_jenkins(
    session: Any,
    base_url: str,
    state: dict[str, Any],
    *,
    job_names: list[str] | None,
    include_job_config: bool,
    include_builds: bool,
    initial_builds: int,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Fetch changed Jenkins documents and return a copy of the next cursor state."""
    next_state = json.loads(json.dumps(state))
    instances = next_state.setdefault("instances", {})
    instance_state = instances.setdefault(base_url, {})
    known_jobs = set(instance_state.get("known_jobs", []))
    last_builds = instance_state.setdefault("last_builds", {})
    pending_builds = instance_state.setdefault("pending_builds", {})
    known_builds = instance_state.setdefault("known_builds", {})
    config_hashes = instance_state.setdefault("config_hashes", {})

    jobs = _list_jobs(session, base_url)
    if job_names is not None:
        requested = set(job_names)
        jobs = {name: job for name, job in jobs.items() if name in requested}

    # An unscoped empty response can mean access was reduced. Preserve the prior
    # corpus in that ambiguous case; a caller-selected scope can be reconciled.
    if known_jobs and not jobs and job_names is None:
        logger.warning("Jenkins: empty job inventory; preserving prior state and documents.")
        return [], next_state

    rows: list[dict[str, Any]] = []
    current_jobs = set(jobs)
    for full_name, job in sorted(jobs.items()):
        job_id = _job_id(base_url, full_name)
        if include_job_config:
            config = _job_config(job)
            config_hash = hashlib.sha256(config["content"].encode("utf-8")).hexdigest()
            if config_hashes.get(full_name) != config_hash:
                rows.append({**config, "id": job_id, "_deleted": False})
                config_hashes[full_name] = config_hash

        if include_builds:
            last_number = _last_build_number(job)
            if last_number is not None:
                prior_number = last_builds.get(full_name)
                if prior_number is None:
                    first_number = max(1, last_number - initial_builds + 1)
                else:
                    first_number = prior_number + 1

                pending = {int(number) for number in pending_builds.get(full_name, [])}
                build_numbers = sorted(pending | set(range(first_number, last_number + 1)))
                for number in build_numbers:
                    build_url = _job_url(job) + f"{number}/"
                    try:
                        build = _get_build_json(session, urljoin(build_url, "api/json"))
                    except Exception as exc:
                        # Jenkins can discard old builds under its retention
                        # policy. A missing number is a gap, not a sync failure.
                        if getattr(getattr(exc, "response", None), "status_code", None) == 404:
                            pending.discard(number)
                            logger.info(
                                "Jenkins: build %s #%d is no longer retained.", full_name, number
                            )
                            continue
                        raise
                    if build.get("building") or not build.get("result"):
                        pending.add(number)
                        continue

                    pending.discard(number)
                    row = _build_to_row(session, base_url, full_name, build, build_url)
                    rows.append(row)
                    known = set(known_builds.get(full_name, []))
                    known.add(number)
                    known_builds[full_name] = sorted(known)

                last_builds[full_name] = max(last_number, int(prior_number or 0))
                pending_builds[full_name] = sorted(pending)

    deleted_jobs = known_jobs - current_jobs
    for full_name in sorted(deleted_jobs):
        rows.append({"id": _job_id(base_url, full_name), "_deleted": True})
        for number in known_builds.pop(full_name, []):
            rows.append({"id": _build_id(base_url, full_name, number), "_deleted": True})
        last_builds.pop(full_name, None)
        pending_builds.pop(full_name, None)
        config_hashes.pop(full_name, None)

    instance_state["known_jobs"] = sorted(current_jobs)
    logger.info(
        "Jenkins: synced %d job(s), emitted %d document(s), forgot %d job(s).",
        len(current_jobs),
        len(rows),
        len(deleted_jobs),
    )
    return rows, next_state


def _list_jobs(session: Any, base_url: str) -> dict[str, dict[str, Any]]:
    """List visible jobs, recursively querying only Jenkins folders."""
    root = _get_jobs_json(session, urljoin(base_url + "/", "api/json"))
    found: dict[str, dict[str, Any]] = {}
    _collect_jobs(session, base_url, root.get("jobs", []), found)
    return found


def _collect_jobs(
    session: Any, base_url: str, entries: list[dict[str, Any]], found: dict[str, dict[str, Any]]
) -> None:
    for entry in entries:
        url = _job_url(entry)
        _validate_same_origin(base_url, url)
        full_name = entry.get("fullName") or entry.get("name")
        if not full_name:
            continue
        job_class = entry.get("_class", "")
        is_folder = any(
            folder_type in job_class for folder_type in ("Folder", "MultiBranchProject")
        )
        if not is_folder:
            found[str(full_name)] = entry
        if is_folder:
            folder = _get_jobs_json(session, urljoin(url, "api/json"))
            _collect_jobs(session, base_url, folder.get("jobs", []), found)


def _get_jobs_json(session: Any, url: str) -> dict[str, Any]:
    """Fetch a Jenkins container and its immediate jobs."""
    response = session.get(url, params={"tree": _JOB_LIST_TREE, "depth": 1}, timeout=(5, 30))
    response.raise_for_status()
    data = response.json()
    if not isinstance(data, dict):
        raise ValueError("Jenkins returned an invalid job inventory object.")
    return data


def _validate_same_origin(base_url: str, url: str) -> None:
    base, candidate = urlsplit(base_url), urlsplit(url)
    if (candidate.scheme, candidate.netloc) != (base.scheme, base.netloc):
        raise ValueError("Jenkins returned a job URL outside the configured instance.")


def _job_url(job: dict[str, Any]) -> str:
    url = job.get("url")
    if not isinstance(url, str) or not url:
        raise ValueError("Jenkins returned a job without a URL.")
    return url.rstrip("/") + "/"


def _last_build_number(job: dict[str, Any]) -> int | None:
    last_build = job.get("lastBuild")
    if not isinstance(last_build, dict) or last_build.get("number") is None:
        return None
    return int(last_build["number"])


def _job_id(base_url: str, full_name: str) -> str:
    return f"{base_url}::job::{full_name}"


def _build_id(base_url: str, full_name: str, number: int) -> str:
    return f"{base_url}::job::{full_name}::build::{number}"


def _job_config(job: dict[str, Any]) -> dict[str, str]:
    """Render an allowlisted job summary; never fetch raw ``config.xml``."""
    full_name = str(job.get("fullName") or job.get("name") or "")
    description = str(job.get("description") or "")
    job_type = str(job.get("_class") or "unknown")
    disabled = bool(job.get("disabled", False))
    buildable = bool(job.get("buildable", False))
    content = (
        f"Jenkins job: {full_name}\n"
        f"Type: {job_type}\n"
        f"Description: {description}\n"
        f"Disabled: {disabled}\n"
        f"Buildable: {buildable}"
    )
    return {"title": f"Jenkins job {full_name}", "content": content, "url": _job_url(job)}


def _build_to_row(
    session: Any,
    base_url: str,
    full_name: str,
    build: dict[str, Any],
    build_url: str,
) -> dict[str, Any]:
    number = int(build["number"])
    result = str(build["result"])
    title = str(build.get("displayName") or f"#{number}")
    content = (
        f"Jenkins job: {full_name}\n"
        f"Build: {title} (#{number})\n"
        f"Result: {result}\n"
        f"Timestamp: {build.get('timestamp', '')}\n"
        f"Duration (ms): {build.get('duration', '')}"
    )
    if result == "FAILURE":
        console = _get_console_text(session, urljoin(build_url, "consoleText"))
        if console:
            content += f"\n\nFailed build console output:\n{console}"
    return {
        "id": _build_id(base_url, full_name, number),
        "title": f"{full_name} {title}",
        "content": content,
        "url": build.get("url") or build_url,
        "_deleted": False,
    }
