"""DLT source for dbt Cloud model documentation, lineage, and run outcomes.

Fetches, for one or more selected jobs/environments/projects, the latest
successful manifest (and, if available, catalog) per environment, plus a
sliding window of each selected job's newest finished runs, and yields them
as a single dlt resource for cognee's ingestion pipeline.

Like the Bitbucket connector, documents are ingested as *normal documents*:
the source declares ``cognee_document_source = "dbt_cloud"``, so
``resolve_dlt_sources`` tags each row ``external_metadata["source"] =
"dbt_cloud"`` (not ``"dlt"``) and routes it through the standard cognify
entity-extraction pipeline -- the right treatment for prose (model
descriptions, lineage, run failure messages) -- instead of the
deterministic dlt-row schema-context path.

Two document families:

* **Definition documents** -- one per in-scope model/source/seed/snapshot/
  exposure/metric, built from the manifest (plus catalog column types when
  available). Stable id ``dbt-cloud:{account_id}:{environment_id}:
  {unique_id}`` -- deliberately excludes any run id, so re-fetching the
  same unchanged node from a later run's manifest does not create a
  duplicate document. The manifest used is the one from each selected
  environment's latest successful run among its selected jobs -- this is
  the authoritative "current definition" source, not something walked via
  per-node API calls (the manifest already holds the full lineage graph).
* **Run-outcome documents** -- one per finished run of a selected job,
  built from the run record plus ``run_results.json`` merged across every
  step that executed nodes. Stable id ``dbt-cloud:{account_id}:run:
  {run_id}``.

Sync model: incremental with a periodic full reconciliation pass, mirroring
the Bitbucket connector.

* **Incremental pass** (the common case): one ``/runs/`` listing per job,
  stopping once a finished run is older than that job's stored cursor.
  Definitions only refresh when an environment's latest successful run
  actually changed; run outcomes use a sliding window of the newest
  ``max_runs_per_job`` finished runs, so runs that age out of the window
  are forgotten.
* **Full pass** (on the first run, whenever the resolved configuration
  changes, or every ``full_sync_every`` runs): every selected job's full
  run history (bounded) is rescanned, every environment's definitions are
  re-rendered from scratch, and anything that fell out of scope --
  including whole jobs/environments no longer selected -- is tombstoned.

Bitbucket pull requests can never be deleted; dbt Cloud runs can be, but
there is no ``state`` field on the run object itself to detect that after
the fact -- the runs-list endpoint's own ``state`` query parameter
defaults to ``all`` (confirmed against the dbt Cloud OpenAPI v2 spec),
which would otherwise include deleted runs. This connector always passes
``state=active`` explicitly, so a run deleted upstream simply stops
appearing in the next listing and is tombstoned the same way any other
vanished run is -- no special-case "deleted run" handling needed.

``write_disposition="merge"`` (declared on the dlt resource below) is what
makes an unchanged row's upsert a no-op and what makes a row with
``_deleted=True`` actually get removed from the dlt destination -- but it
is NOT automatically what ``cognee.remember()``/``cognee.add()`` use: that
routing is controlled by a kwarg the *caller* passes to those functions,
not by anything declared on the dlt resource itself (confirmed in the
installed ``cognee`` package, same finding as the Bitbucket connector's
own research: ``resolve_dlt_sources`` resolves ``write_disposition`` from
its own ``**kwargs``, defaulting to ``"replace"`` when the caller didn't
pass one). Every caller MUST therefore pass ``write_disposition="merge"``
and ``primary_key="id"`` explicitly -- see the README and example.
"""

import hashlib
import json
import os
import random
import tempfile
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from cognee.shared.logging_utils import get_logger
from cognee.tasks.ingestion.dlt_utils import DOCUMENT_SOURCE_ATTR

logger = get_logger("dbt_cloud_connector")

DBT_CLOUD_TABLE_NAME = "dbt_cloud_documents"
DBT_CLOUD_SOURCE_NAME = "dbt_cloud"

# Administrative API v2 owns Jobs/Runs/Artifacts; this connector never calls
# v3 or the Discovery API (GraphQL) -- see the research report for why
# manifest-parsing is preferred over Discovery API for this use case.
_API_BASE_PATH = "/api/v2"
_DEFAULT_HOST = "cloud.getdbt.com"
_DEFAULT_FULL_SYNC_EVERY = 10

_ALL_RESOURCE_TYPES = ("models", "sources", "seeds", "snapshots", "exposures", "metrics")
# Maps a resource_types entry to (manifest top-level key, manifest resource_type
# filter). A None filter means "every entry in that collection counts" (sources/
# exposures/metrics aren't mixed with other kinds in their own collections).
_RESOURCE_TYPE_TO_MANIFEST_KEY = {
    "models": ("nodes", "model"),
    "seeds": ("nodes", "seed"),
    "snapshots": ("nodes", "snapshot"),
    "sources": ("sources", None),
    "exposures": ("exposures", None),
    "metrics": ("metrics", None),
}
# Confirmed against the dbt Cloud OpenAPI v2 spec's JobTypeEnum: ci, scheduled,
# merge, other. CI and merge-triggered jobs are excluded by default since they
# run on ephemeral branches/PRs, not a stable definition of the project.
_CI_JOB_TYPES = {"ci", "merge"}
# resource_type values that make sense as lineage context (excludes test/macro/
# analysis, which are plumbing/validation, never something else's upstream).
_LINEAGE_RESOURCE_TYPES = {"model", "seed", "snapshot", "source", "exposure", "metric"}
_LINEAGE_CAP = 100
_FAILING_NODE_CAP = 20
_MESSAGE_TRIM = 500
# Generic-test kwargs that are internal plumbing, not useful summary text:
# "column_name" is already shown separately, and "model" is a raw jinja
# macro-call string (e.g. "{{ get_where_subquery(ref('x')) }}") -- confirmed
# present on every generic test by inspecting a real dbt-core manifest.json
# (dbt-labs/jaffle-shop, dbt-core 1.12.5) during this connector's research.
_TEST_KWARGS_TO_SKIP = {"column_name", "model"}

# Confirmed against the dbt Cloud OpenAPI v2 spec's RunResponse.status description:
# "1: Queued, 2: Starting, 3: Running, 10: Success, 20: Error, 30: Cancelled".
_RUN_STATUS_SUCCESS = 10
_FINISHED_RUN_STATUSES = {10, 20, 30}
_STATUS_LABELS = {10: "Success", 20: "Error", 30: "Cancelled"}
# A full/first pass keeps scanning past max_runs_per_job, looking for a
# successful run to source the manifest from, up to this many items --
# roughly "10 pages" at the 100-item page size _paginate_offset requests.
_MAX_SUCCESS_SEARCH_ITEMS = 10 * 100

_PAGE_LEN = 100
# Runaway-loop guard for _paginate_offset: 1000 pages at 100 items/page is
# 100,000 items for one listing -- far beyond any real account, so hitting
# this means the API's pagination contract broke, not that there's legitimately
# more data.
_MAX_PAGINATION_PAGES = 1000
_MAX_RETRIES = 5
# Confirmed against docs.getdbt.com/docs/dbt-apis/rate-limits: Admin API is
# 5,000 req/min; a 429 enforces a flat 5-minute cooldown (no documented
# Retry-After header), after which requests work again.
_RATE_LIMIT_MAX_RETRIES = 2
_RATE_LIMIT_COOLDOWN_SECONDS = 300

_EXTRA_HINT = (
    'The dbt Cloud connector requires the "dbt-cloud" extra: '
    'pip install "cognee[dbt-cloud]" (provides dlt and requests).'
)

_PERMISSION_HINT = (
    "The token needs at least read-only access to Jobs, Runs, and Artifacts -- e.g. the "
    "'Read-Only' permission set / license (Enterprise: combine with 'Job Viewer'; Starter: "
    "assign the 'Read-only' license type to the user who creates the token)."
)


@dataclass(frozen=True)
class _Config:
    account_id: int
    base_url: str
    project_ids: tuple[int, ...] | None
    environment_ids: tuple[int, ...] | None
    job_ids: tuple[int, ...] | None
    resource_types: tuple[str, ...]
    include_packages: bool
    include_sql: bool
    include_catalog: bool
    include_run_outcomes: bool
    include_ci_jobs: bool
    max_runs_per_job: int
    max_artifact_mb: int
    full_sync_every: int


def dbt_cloud_source(
    account_id: int | str | None = None,
    *,
    api_token: str | None = None,
    host: str | None = None,
    project_ids: list[int] | None = None,
    environment_ids: list[int] | None = None,
    job_ids: list[int] | None = None,
    resource_types: tuple[str, ...] = _ALL_RESOURCE_TYPES,
    include_packages: bool = False,
    include_sql: bool = False,
    include_catalog: bool = True,
    include_run_outcomes: bool = True,
    include_ci_jobs: bool = False,
    max_runs_per_job: int = 50,
    max_artifact_mb: int = 200,
    full_sync_every: int = _DEFAULT_FULL_SYNC_EVERY,
    session: Any = None,
):
    """Create a dlt source that yields dbt Cloud definition and run-outcome documents.

    Args:
        account_id: dbt Cloud account id. Falls back to ``DBT_CLOUD_ACCOUNT_ID``.
        api_token: A personal access token or service account token (the
            deprecated user API keys are not supported). Falls back to
            ``DBT_CLOUD_API_TOKEN``.
        host: The account-specific access URL host (e.g. ``abc123.us1.dbt.com``),
            with or without an ``https://`` scheme. Falls back to
            ``DBT_CLOUD_HOST``, defaulting to ``cloud.getdbt.com`` (scheduled
            for deprecation 2027-02-03 -- see the README).
        project_ids: Restrict job discovery to these project ids.
        environment_ids: Restrict job discovery to these environment ids
            (takes precedence over ``project_ids`` if both are given --
            ``project_ids`` then filters the environment's jobs locally).
        job_ids: Sync exactly these jobs (an id that doesn't exist or is
            inactive is a configuration error, not a silent skip).
        resource_types: Which manifest node kinds to ingest as definition
            documents. Subset of ``("models", "sources", "seeds",
            "snapshots", "exposures", "metrics")``.
        include_packages: When False (default), only nodes whose
            ``package_name`` matches the manifest's own root project are
            ingested; installed-package nodes are skipped.
        include_sql: When True, include each node's raw SQL in its document.
            Off by default (SQL can be large and sometimes sensitive).
        include_catalog: Fetch ``catalog.json`` alongside the manifest to
            enrich column documentation with warehouse column types.
        include_run_outcomes: Also ingest one document per finished run,
            as a sliding window of each job's newest ``max_runs_per_job``.
        include_ci_jobs: Include CI/merge-triggered jobs. Off by default --
            they run on ephemeral branches/PRs, not a stable project state.
        max_runs_per_job: Size of the per-job run-outcome sliding window.
        max_artifact_mb: Abort rather than buffer an artifact larger than this.
        full_sync_every: Run a full reconciliation pass every this many runs
            (1 means every run is a full pass). A full pass also always
            happens on the first run and whenever the resolved
            configuration (ids, resource_types, include_* flags) changes.
        session: Pre-built ``requests`` session (mainly a test-injection
            point); when omitted one is built from ``api_token``.

    Returns:
        A dlt source suitable for ``cognee.add(...)`` / ``cognee.remember(...)``.
    """
    resolved_account_id = _resolve_account_id(account_id)
    base_url = _normalize_host(host or os.environ.get("DBT_CLOUD_HOST"))

    if not resource_types:
        raise ValueError("resource_types must not be empty.")
    invalid_types = sorted(set(resource_types) - set(_ALL_RESOURCE_TYPES))
    if invalid_types:
        raise ValueError(
            f"Invalid resource_types {invalid_types}; must be a subset of {_ALL_RESOURCE_TYPES}."
        )

    project_ids = _validate_ids("project_ids", project_ids)
    environment_ids = _validate_ids("environment_ids", environment_ids)
    job_ids = _validate_ids("job_ids", job_ids)
    if not (project_ids or environment_ids or job_ids):
        raise ValueError(
            "dbt_cloud_source requires at least one of project_ids, environment_ids, or job_ids."
        )
    if max_runs_per_job < 1:
        raise ValueError("max_runs_per_job must be >= 1.")
    if max_artifact_mb < 1:
        raise ValueError("max_artifact_mb must be >= 1.")
    if full_sync_every < 1:
        raise ValueError("full_sync_every must be >= 1 (1 means every run is a full pass).")

    try:
        import dlt
    except ImportError as exc:
        raise ImportError(_EXTRA_HINT) from exc

    if session is None:
        session = _make_session(api_token)

    config = _Config(
        account_id=resolved_account_id,
        base_url=base_url,
        project_ids=project_ids,
        environment_ids=environment_ids,
        job_ids=job_ids,
        resource_types=tuple(resource_types),
        include_packages=include_packages,
        include_sql=include_sql,
        include_catalog=include_catalog,
        include_run_outcomes=include_run_outcomes,
        include_ci_jobs=include_ci_jobs,
        max_runs_per_job=max_runs_per_job,
        max_artifact_mb=max_artifact_mb,
        full_sync_every=full_sync_every,
    )

    @dlt.resource(
        name=DBT_CLOUD_TABLE_NAME,
        primary_key="id",
        write_disposition="merge",
        # `_deleted` is a boolean hard-delete marker: rows where it is True
        # are removed from the dlt destination on merge, which propagates
        # the deletion through cognee's orphan_cleanup. Callers must pass
        # write_disposition="merge" explicitly to remember()/add() too (see
        # the module docstring) -- this decorator value alone is not enough.
        columns={"_deleted": {"data_type": "bool", "hard_delete": True}},
    )
    def dbt_cloud_documents():
        yield from _iter_rows(session, config, dlt.current.resource_state())

    @dlt.source(name=DBT_CLOUD_SOURCE_NAME)
    def _dbt_cloud():
        return dbt_cloud_documents

    source = _dbt_cloud()
    # Opt into the document ingestion path (row -> text document -> cognify).
    # resolve_dlt_sources reads this marker; it never imports this connector.
    setattr(source, DOCUMENT_SOURCE_ATTR, DBT_CLOUD_SOURCE_NAME)
    return source


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def _resolve_account_id(account_id: int | str | None) -> int:
    account_id = account_id if account_id is not None else os.environ.get("DBT_CLOUD_ACCOUNT_ID")
    if account_id is None or account_id == "":
        raise ValueError("dbt_cloud_source requires account_id (or DBT_CLOUD_ACCOUNT_ID).")
    try:
        resolved = int(account_id)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"account_id must be a positive integer, got {account_id!r}.") from exc
    if resolved <= 0:
        raise ValueError(f"account_id must be a positive integer, got {account_id!r}.")
    return resolved


def _validate_ids(name: str, ids: list[int] | None) -> tuple[int, ...] | None:
    if ids is None:
        return None
    if not ids or any(isinstance(i, bool) or not isinstance(i, int) or i <= 0 for i in ids):
        raise ValueError(f"{name}, if given, must be a non-empty list of positive integers.")
    return tuple(ids)


def _normalize_host(host: str | None) -> str:
    """Normalize a dbt Cloud access-URL host into a bare ``https://<host>`` base.

    Accepts a bare host (``abc123.us1.dbt.com``), a host with an explicit
    ``https://`` scheme, and/or a trailing slash. Rejects ``http://`` and
    any path, query string, or embedded credentials, since those would
    silently change which endpoint every request hits rather than failing
    loudly on a config mistake.
    """
    host = (host or _DEFAULT_HOST).strip()
    if host.lower().startswith("http://"):
        raise ValueError("dbt_cloud_source: host must use https, not http.")
    if host.lower().startswith("https://"):
        host = host[len("https://") :]
    host = host.rstrip("/")
    if not host:
        raise ValueError("dbt_cloud_source: host must not be empty.")
    if any(ch in host for ch in "/?@#"):
        raise ValueError(
            "dbt_cloud_source: host must be a bare hostname (e.g. 'cloud.getdbt.com' or "
            f"'abc123.us1.dbt.com'), not a URL with a path/query/credentials: {host!r}."
        )
    return f"https://{host}"


# ---------------------------------------------------------------------------
# Auth / session
# ---------------------------------------------------------------------------


def _resolve_api_token(api_token: str | None) -> str:
    api_token = api_token or os.environ.get("DBT_CLOUD_API_TOKEN")
    if not api_token:
        raise ValueError(
            "dbt_cloud_source requires an API token: pass api_token= or set "
            "DBT_CLOUD_API_TOKEN. Use a personal access token or a service account token; "
            "user API keys are deprecated."
        )
    return api_token


def _make_session(api_token: str | None) -> Any:
    """Build a ``requests`` session authenticated against the dbt Cloud Admin API.

    ``requests`` is imported lazily so it stays an optional dependency
    (``pip install "cognee[dbt-cloud]"``).
    """
    try:
        import requests
    except ImportError as exc:
        raise ImportError(_EXTRA_HINT) from exc

    token = _resolve_api_token(api_token)
    session = requests.Session()
    session.headers["Authorization"] = f"Bearer {token}"
    session.headers["Accept"] = "application/json"
    return session


# ---------------------------------------------------------------------------
# Retrying GET / artifact download
# ---------------------------------------------------------------------------


def _sleep(seconds: float) -> None:
    """Thin wrapper around ``time.sleep`` so tests can monkeypatch it to run instantly."""
    time.sleep(seconds)


def _retry_delay(attempt: int) -> float:
    """Exponential backoff with jitter for transient 5xx retries."""
    return (2**attempt) + random.random()


def _get_json(session: Any, url: str, params: Any = None) -> dict:
    """GET a dbt Cloud API URL and return the parsed ``{data, extra, status}`` envelope.

    502/503/504 are retried with backoff up to ``_MAX_RETRIES`` times. 429
    waits the documented flat 5-minute cooldown and retries at most
    ``_RATE_LIMIT_MAX_RETRIES`` times. 401/403/404 raise with a specific,
    credential-free message; a connection/DNS failure raises hinting at a
    wrong host/region; a 200 response whose own ``status.is_success`` is
    false raises with its ``user_message``. Nothing is ever swallowed: a
    failure here must abort the whole sync so a partial listing can never
    be mistaken for a deletion.
    """
    server_error_attempts = 0
    rate_limit_attempts = 0
    while True:
        try:
            response = session.get(url, params=params)
        except Exception as exc:
            raise RuntimeError(
                f"dbt Cloud API request to {url} failed to connect: {exc}. Check that `host` "
                "points at the correct account-specific access URL/region (Account settings > "
                "Account information)."
            ) from exc

        status = response.status_code
        if status == 200:
            payload = response.json()
            status_block = payload.get("status") or {}
            if status_block.get("is_success") is False:
                message = status_block.get("user_message") or "unknown error"
                raise RuntimeError(f"dbt Cloud API error for {url}: {message}")
            return payload
        if status == 401:
            raise RuntimeError(
                "dbt Cloud API authentication failed (401). Use an account-scoped personal "
                "access token or a service account token; user API keys are deprecated."
            )
        if status == 403:
            raise RuntimeError(
                f"dbt Cloud API authorization failed (403) for {url}. {_PERMISSION_HINT}"
            )
        if status == 404:
            raise RuntimeError(f"dbt Cloud API resource not found: {url}")

        if status == 429:
            rate_limit_attempts += 1
            if rate_limit_attempts > _RATE_LIMIT_MAX_RETRIES:
                raise RuntimeError(
                    f"dbt Cloud API rate limit exceeded for {url} after "
                    f"{_RATE_LIMIT_MAX_RETRIES} retries."
                )
            logger.warning(
                "dbt Cloud: %s rate-limited (429), waiting %ds before retrying.",
                url,
                _RATE_LIMIT_COOLDOWN_SECONDS,
            )
            _sleep(_RATE_LIMIT_COOLDOWN_SECONDS)
            continue

        if status in (502, 503, 504):
            server_error_attempts += 1
            if server_error_attempts >= _MAX_RETRIES:
                raise RuntimeError(
                    f"dbt Cloud API request to {url} failed after {_MAX_RETRIES} attempts "
                    f"(last status {status})."
                )
            delay = _retry_delay(server_error_attempts - 1)
            logger.warning(
                "dbt Cloud: %s returned %d, retrying in %.1fs (%d/%d).",
                url,
                status,
                delay,
                server_error_attempts,
                _MAX_RETRIES,
            )
            _sleep(delay)
            continue

        response.raise_for_status()
        raise RuntimeError(
            f"Unexpected dbt Cloud API response from {url}: status {status}."
        )  # pragma: no cover


def _get_artifact(
    session: Any, url: str, params: Any, max_bytes: int, *, optional: bool
) -> dict | None:
    """Stream-download and parse a run artifact JSON file.

    Downloads into a ``SpooledTemporaryFile`` while counting bytes (never
    buffering the whole response in memory twice), raising if
    ``max_artifact_mb`` is exceeded. Returns ``None`` only when ``optional``
    is True and the artifact 404s (catalog.json/run_results.json may not
    exist for every run/step); a 404 when ``optional`` is False
    (manifest.json) is always an error.
    """
    try:
        response = session.get(url, params=params, stream=True)
    except Exception as exc:
        raise RuntimeError(f"dbt Cloud artifact request to {url} failed to connect: {exc}") from exc

    if response.status_code == 404:
        if optional:
            return None
        raise RuntimeError(f"dbt Cloud API resource not found: {url}")
    if response.status_code != 200:
        response.raise_for_status()
        raise RuntimeError(
            f"Unexpected dbt Cloud API response from {url}: status {response.status_code}."
        )

    size = 0
    with tempfile.SpooledTemporaryFile(max_size=10 * 1024 * 1024, mode="w+b") as buffer:
        for chunk in response.iter_content(chunk_size=65536):
            size += len(chunk)
            if size > max_bytes:
                raise RuntimeError(
                    f"dbt Cloud artifact at {url} exceeded max_artifact_mb="
                    f"{max_bytes // (1024 * 1024)}; increase max_artifact_mb if this is expected."
                )
            buffer.write(chunk)
        buffer.seek(0)
        return json.load(buffer)


# ---------------------------------------------------------------------------
# Pagination
# ---------------------------------------------------------------------------


def _paginate_offset(session: Any, url: str, params: dict):
    """Yield items across dbt Cloud's offset/limit pagination.

    Requests ``limit=100`` pages and advances ``offset`` by the number of
    items actually returned. Stops when:

    * a page is empty, or
    * ``extra.pagination.total_count`` is present and an ``int``, and
      ``offset`` has reached it, or
    * ``total_count`` is missing/not an ``int`` (never trusted alone) and
      the page came back short of the requested ``limit`` -- the standard
      "last page" signal for offset/limit APIs.

    Two defensive guards bound a misbehaving or malformed API response
    from looping forever: raises if a non-empty page is returned but
    ``offset`` fails to advance, and raises if more than
    ``_MAX_PAGINATION_PAGES`` pages are fetched for one listing.
    """
    offset = 0
    params = dict(params or {})
    params["limit"] = _PAGE_LEN
    for _page in range(_MAX_PAGINATION_PAGES):
        params["offset"] = offset
        payload = _get_json(session, url, params=params)
        items = payload.get("data") or []
        if not items:
            return
        yield from items

        pagination = (payload.get("extra") or {}).get("pagination") or {}
        total_count = pagination.get("total_count")
        previous_offset = offset
        offset += len(items)
        if offset <= previous_offset:
            raise RuntimeError(f"dbt Cloud pagination did not advance at {url}.")
        if isinstance(total_count, int):
            if offset >= total_count:
                return
        elif len(items) < _PAGE_LEN:
            return  # total_count absent/malformed: a short page means "last page"

    raise RuntimeError(
        f"dbt Cloud pagination exceeded {_MAX_PAGINATION_PAGES} pages at {url}; "
        "aborting to avoid an unbounded listing."
    )


# ---------------------------------------------------------------------------
# Jobs
# ---------------------------------------------------------------------------


def _get_job(session: Any, config: _Config, job_id: int) -> dict:
    url = f"{config.base_url}{_API_BASE_PATH}/accounts/{config.account_id}/jobs/{job_id}/"
    payload = _get_json(session, url)
    job = payload.get("data") or {}
    if job.get("state") != 1:
        raise RuntimeError(f"dbt Cloud job {job_id} is deleted or inactive; check job_ids.")
    return job


def _list_jobs_by_scope(session: Any, config: _Config):
    url = f"{config.base_url}{_API_BASE_PATH}/accounts/{config.account_id}/jobs/"
    if config.environment_ids:
        for environment_id in config.environment_ids:
            for job in _paginate_offset(session, url, {"environment_id": environment_id}):
                if config.project_ids and job.get("project_id") not in config.project_ids:
                    continue
                yield job
        return
    for project_id in config.project_ids:
        yield from _paginate_offset(session, url, {"project_id": project_id})


def _resolve_jobs(session: Any, config: _Config) -> list[dict]:
    """Resolve the final list of job dicts to sync, deduped by id.

    Explicit ``job_ids`` are fetched directly -- an id that doesn't exist
    or is inactive is a configuration error, not a silent skip. Otherwise,
    jobs are listed per ``environment_id`` (filtered to ``project_ids``
    locally if also given) or per ``project_id``. System jobs are always
    skipped; CI/merge jobs are skipped unless ``include_ci_jobs``. An
    explicitly-given job id that turns out to be a system or CI/merge job
    is dropped with a warning (not an error) naming how to include it.
    """
    explicit = bool(config.job_ids)
    if explicit:
        jobs = [_get_job(session, config, job_id) for job_id in config.job_ids]
    else:
        jobs = list(_list_jobs_by_scope(session, config))

    seen: dict[int, dict] = {}
    for job in jobs:
        if job.get("is_system"):
            if explicit:
                logger.warning(
                    "dbt Cloud: job %s was explicitly requested but is a system job; dropping it.",
                    job["id"],
                )
            continue
        job_type = job.get("job_type")
        if not config.include_ci_jobs and job_type in _CI_JOB_TYPES:
            if explicit:
                logger.warning(
                    "dbt Cloud: job %s was explicitly requested but is a %s job; dropping it. "
                    "Pass include_ci_jobs=True to include ci/merge jobs.",
                    job["id"],
                    job_type,
                )
            continue
        seen[job["id"]] = job

    resolved = sorted(seen.values(), key=lambda j: j["id"])
    logger.info(
        "dbt Cloud: resolved %d job(s): %s",
        len(resolved),
        ", ".join(f"{j['id']}:{j.get('name', '?')}" for j in resolved),
    )
    return resolved


# ---------------------------------------------------------------------------
# Datetime / config-hash helpers
# ---------------------------------------------------------------------------


def _parse_dt(value: str) -> datetime:
    """Parse a dbt Cloud timestamp into a timezone-aware datetime.

    Never compare these as strings: a missing fractional-seconds component,
    or a ``Z`` suffix vs. an explicit offset, would sort incorrectly as
    text even though they compare correctly as datetimes.
    """
    return datetime.fromisoformat(value)


def _config_hash(config: _Config) -> str:
    """Stable hash of the parts of the config that change what's in scope.

    Deliberately excludes ``base_url``/host and the token (neither is a
    credential that should ever be persisted, and host isn't a credential
    but also isn't part of "scope") and ``max_artifact_mb``/
    ``full_sync_every`` (neither changes which rows should exist, only how
    they're fetched/scheduled). A changed hash forces a full pass so every
    row gets re-evaluated under the new scope.
    """
    payload = {
        "account_id": config.account_id,
        "project_ids": sorted(config.project_ids) if config.project_ids else None,
        "environment_ids": sorted(config.environment_ids) if config.environment_ids else None,
        "job_ids": sorted(config.job_ids) if config.job_ids else None,
        "resource_types": sorted(config.resource_types),
        "include_packages": config.include_packages,
        "include_sql": config.include_sql,
        "include_catalog": config.include_catalog,
        "include_run_outcomes": config.include_run_outcomes,
        "include_ci_jobs": config.include_ci_jobs,
        "max_runs_per_job": config.max_runs_per_job,
    }
    blob = json.dumps(payload, sort_keys=True)
    return hashlib.sha256(blob.encode()).hexdigest()


# ---------------------------------------------------------------------------
# Run scanning (one /runs/ listing per job per pass)
# ---------------------------------------------------------------------------


def _scan_job_runs(
    session: Any, config: _Config, job: dict, cursor: str | None
) -> tuple[list[dict], dict | None]:
    """One ``/runs/`` listing for a job, serving both the environment's
    manifest-source selection and this job's run-outcome sliding window.

    ``state=active`` is passed explicitly: confirmed against the dbt Cloud
    OpenAPI v2 spec that the runs-list endpoint's own ``state`` filter
    defaults to ``all`` (unlike jobs/projects/environments, which default
    to ``active``) -- so without this, deleted runs would be included.

    Returns ``(finished_runs, latest_success)``.

    * ``cursor is None`` (full/first pass, or a job never seen before):
      collects the newest ``max_runs_per_job`` finished runs for the
      outcomes window, but keeps scanning past that cap (bounded at
      ``_MAX_SUCCESS_SEARCH_ITEMS`` total items examined) if no SUCCESS
      has been found yet -- the environment's active manifest-source run
      may be older than the outcomes window.
    * ``cursor`` given (incremental): stops once a finished run is older
      than it. Never breaks on the very first item examined, and
      permanently disables the early stop (scanning every remaining page)
      the moment the sequence is found not to be sorted as expected.
    """
    url = f"{config.base_url}{_API_BASE_PATH}/accounts/{config.account_id}/runs/"
    params = {"job_definition_id": job["id"], "order_by": "-finished_at", "state": "active"}
    cursor_dt = _parse_dt(cursor) if cursor else None

    finished_runs: list[dict] = []
    latest_success: dict | None = None
    previous_dt: datetime | None = None
    trust_sort = True
    examined = 0

    for run in _paginate_offset(session, url, params):
        status, finished_at = run.get("status"), run.get("finished_at")
        if status not in _FINISHED_RUN_STATUSES or not finished_at:
            continue  # unfinished runs never influence ordering/window decisions

        updated_dt = _parse_dt(finished_at)
        is_first = previous_dt is None
        if trust_sort and not is_first and updated_dt > previous_dt:
            logger.warning(
                "dbt Cloud: run listing for job %s was not sorted by -finished_at as "
                "expected; scanning every page instead of stopping early.",
                job["id"],
            )
            trust_sort = False
        previous_dt = updated_dt
        examined += 1

        out_of_window = cursor_dt is not None and updated_dt < cursor_dt
        if not out_of_window:
            finished_runs.append(run)
            if status == _RUN_STATUS_SUCCESS and latest_success is None:
                latest_success = run
        elif trust_sort and not is_first:
            break  # incremental: past the cursor, nothing older is relevant

        if cursor_dt is None:
            have_window = len(finished_runs) >= config.max_runs_per_job
            search_exhausted = examined >= _MAX_SUCCESS_SEARCH_ITEMS
            if (have_window and latest_success is not None) or search_exhausted:
                if latest_success is None:
                    logger.warning(
                        "dbt Cloud: no successful run found for job %s within %d run(s) scanned.",
                        job["id"],
                        examined,
                    )
                break

    if cursor_dt is None:
        finished_runs = finished_runs[: config.max_runs_per_job]
    return finished_runs, latest_success


# ---------------------------------------------------------------------------
# Manifest / catalog fetch
# ---------------------------------------------------------------------------


def _fetch_manifest_and_catalog(
    session: Any, config: _Config, run: dict
) -> tuple[dict, dict | None]:
    """Fetch manifest.json (required) and, if enabled, catalog.json (optional)
    from a run's artifacts. Both default to the run's last step, which is
    exactly right here: `dbt docs generate` (if the job runs it) is always
    the last configured step, and is the only step that produces
    catalog.json; manifest.json from the last step is still the final,
    representative state either way.
    """
    max_bytes = config.max_artifact_mb * 1024 * 1024
    base = (
        f"{config.base_url}{_API_BASE_PATH}/accounts/{config.account_id}/runs/{run['id']}/artifacts"
    )
    manifest = _get_artifact(session, f"{base}/manifest.json", None, max_bytes, optional=False)
    catalog = None
    if config.include_catalog:
        catalog = _get_artifact(session, f"{base}/catalog.json", None, max_bytes, optional=True)
    return manifest, catalog


# ---------------------------------------------------------------------------
# Definition rendering
# ---------------------------------------------------------------------------


def _build_name_index(manifest: dict) -> dict[str, str]:
    """Map every lineage-relevant unique_id to its display name."""
    names: dict[str, str] = {}
    for unique_id, node in (manifest.get("nodes") or {}).items():
        if node.get("resource_type") in _LINEAGE_RESOURCE_TYPES:
            names[unique_id] = node.get("name") or unique_id
    for collection_key in ("sources", "exposures", "metrics"):
        for unique_id, node in (manifest.get(collection_key) or {}).items():
            names[unique_id] = node.get("name") or unique_id
    return names


def _build_tests_index(manifest: dict) -> dict[str, list[str]]:
    """Map a node's unique_id to the generic-test summaries attached to it.

    Verified against a real dbt-core manifest.json (dbt-labs/jaffle-shop,
    dbt-core 1.12.5, manifest schema v12) during this connector's research:
    generic test nodes do carry ``attached_node``, ``column_name``, and
    ``test_metadata.{name,kwargs,namespace}`` exactly as used here. A
    standalone singular test (no schema-level config) has none of these
    and an empty ``depends_on.nodes`` -- it is simply dropped from the
    index, which is correct since it isn't tied to one specific node.
    """
    tests_by_node: dict[str, list[str]] = {}
    for test in (manifest.get("nodes") or {}).values():
        if test.get("resource_type") != "test":
            continue
        attached = test.get("attached_node")
        if not attached:
            depends_on_nodes = (test.get("depends_on") or {}).get("nodes") or []
            if len(depends_on_nodes) == 1:
                attached = depends_on_nodes[0]
        if not attached:
            continue

        test_metadata = test.get("test_metadata") or {}
        test_name = test_metadata.get("name") or test.get("name") or "test"
        column_name = test.get("column_name")
        kwargs = {
            k: v
            for k, v in (test_metadata.get("kwargs") or {}).items()
            if k not in _TEST_KWARGS_TO_SKIP
        }

        summary = test_name
        if column_name:
            summary += f" on column {column_name}"
        if kwargs:
            kwargs_text = ", ".join(f"{k}={v!r}" for k, v in sorted(kwargs.items(), key=str))
            summary += f" ({kwargs_text})"
        tests_by_node.setdefault(attached, []).append(summary)
    return tests_by_node


def _render_lineage(label: str, ids: list[str], names: dict[str, str]) -> str:
    relevant = [i for i in ids if i in names]
    if not relevant:
        return f"{label}: none"
    shown = relevant[:_LINEAGE_CAP]
    text = ", ".join(f"{names[i]} ({i})" for i in shown)
    if len(relevant) > _LINEAGE_CAP:
        text += f" (+{len(relevant) - _LINEAGE_CAP} more)"
    return f"{label}: {text}"


def _definition_doc_id(account_id: int, environment_id, unique_id: str) -> str:
    return f"dbt-cloud:{account_id}:{environment_id}:{unique_id}"


def _node_to_row(
    config: _Config,
    environment_id,
    unique_id: str,
    node: dict,
    resource_type_label: str,
    catalog_nodes: dict,
    parent_map: dict,
    child_map: dict,
    names: dict[str, str],
    tests_by_node: dict[str, list[str]],
) -> dict:
    """Flatten a manifest node into a document row.

    Only identity fields plus a fixed, non-volatile set of metadata and the
    description are rendered -- nothing here changes between two
    successful runs that didn't actually change the node's definition, so
    a no-op resync keeps the same content hash.
    """
    title = f"[{resource_type_label}] {node.get('name') or unique_id}"

    lines = [f"Resource type: {resource_type_label}"]
    materialized = (node.get("config") or {}).get("materialized")
    if materialized:
        lines.append(f"Materialized: {materialized}")

    database = node.get("database")
    schema = node.get("schema")
    alias = node.get("alias") or node.get("identifier")
    if database or schema or alias:
        lines.append(f"Location: {database}.{schema}.{alias}")

    path = node.get("original_file_path") or node.get("path")
    if path:
        lines.append(f"Path: {path}")

    tags = node.get("tags") or []
    if tags:
        lines.append(f"Tags: {', '.join(tags)}")

    meta = node.get("meta") or {}
    if meta:
        lines.append("Meta: " + ", ".join(f"{k}={v}" for k, v in sorted(meta.items(), key=str)))

    for field in ("access", "group", "contract", "deprecation_date"):
        value = node.get(field)
        if value:
            lines.append(f"{field.capitalize()}: {value}")

    columns = node.get("columns") or {}
    catalog_columns = (catalog_nodes.get(unique_id) or {}).get("columns") or {}
    if columns:
        column_lines = []
        for column_name, column in columns.items():
            data_type = column.get("data_type") or (catalog_columns.get(column_name) or {}).get(
                "type"
            )
            column_desc = (column.get("description") or "").strip()
            piece = column_name
            if data_type:
                piece += f" ({data_type})"
            if column_desc:
                piece += f": {column_desc}"
            column_lines.append(f"- {piece}")
        lines.append("Columns:\n" + "\n".join(column_lines))

    tests = tests_by_node.get(unique_id) or []
    if tests:
        lines.append("Tests:\n" + "\n".join(f"- {t}" for t in tests))

    lines.append(_render_lineage("Upstream", parent_map.get(unique_id) or [], names))
    lines.append(_render_lineage("Downstream", child_map.get(unique_id) or [], names))

    if config.include_sql:
        sql = node.get("raw_code") or node.get("raw_sql")
        if sql:
            lines.append("Source SQL:\n```\n" + sql + "\n```")

    description = (node.get("description") or "").strip()
    content = "\n\n".join(lines)
    if description:
        content = f"{description}\n\n{content}"

    return {
        "id": _definition_doc_id(config.account_id, environment_id, unique_id),
        "title": title,
        "content": content,
        "_deleted": False,
    }


def _manifest_to_rows(config: _Config, environment_id, manifest: dict, catalog: dict | None):
    metadata = manifest.get("metadata") or {}
    logger.debug("dbt Cloud: manifest schema version %s", metadata.get("dbt_schema_version"))

    own_project_name = metadata.get("project_name")
    if own_project_name is None:
        logger.warning(
            "dbt Cloud: manifest metadata has no project_name (expected on manifest schema "
            "v10+ / dbt v1.6+); including nodes from every package for environment %s.",
            environment_id,
        )

    catalog_nodes: dict = {}
    if catalog:
        catalog_nodes.update(catalog.get("nodes") or {})
        catalog_nodes.update(catalog.get("sources") or {})

    names = _build_name_index(manifest)
    tests_by_node = _build_tests_index(manifest)
    parent_map = manifest.get("parent_map") or {}
    child_map = manifest.get("child_map") or {}

    for resource_type in config.resource_types:
        collection_key, filter_resource_type = _RESOURCE_TYPE_TO_MANIFEST_KEY[resource_type]
        collection = manifest.get(collection_key) or {}
        for unique_id, node in collection.items():
            if filter_resource_type and node.get("resource_type") != filter_resource_type:
                continue
            if (
                not config.include_packages
                and own_project_name is not None
                and node.get("package_name") != own_project_name
            ):
                continue
            yield _node_to_row(
                config,
                environment_id,
                unique_id,
                node,
                node.get("resource_type") or resource_type[:-1],
                catalog_nodes,
                parent_map,
                child_map,
                names,
                tests_by_node,
            )


def _sync_environment_definitions(
    session: Any,
    config: _Config,
    environment_id,
    success_candidates: list[dict | None],
    prior_env_state: dict,
    is_full: bool,
) -> tuple[list[dict], dict]:
    """Sync one environment's definition documents.

    ``success_candidates`` is one ``latest_success`` run per selected job
    in this environment, from this pass's per-job ``_scan_job_runs`` calls
    (``None`` where a job had no success in its scan window). The
    candidate with the greatest ``finished_at`` is picked -- never a newer
    FAILED run, since only SUCCESS runs are ever candidates at all.

    On an incremental pass, if the picked run's id equals the stored
    ``manifest_run_id``, nothing is fetched or re-rendered (the manifest
    hasn't changed). On a FULL pass, always re-fetch and re-render even if
    the picked run is unchanged, since the resolved configuration (e.g.
    resource_types) may have changed what should be rendered from the same
    manifest -- full passes always reach `config_hash`-changed scope
    refreshes this way without extra logic.

    Returns ``(rows, new_env_state)`` where ``new_env_state`` is
    ``{"manifest_run_id": int | None, "node_ids": [...]}``.
    """
    best_run = None
    for run in success_candidates:
        if run is None:
            continue
        if best_run is None or _parse_dt(run["finished_at"]) > _parse_dt(best_run["finished_at"]):
            best_run = run

    stored_run_id = prior_env_state.get("manifest_run_id")
    stored_node_ids = set(prior_env_state.get("node_ids", []))

    if best_run is None:
        if stored_node_ids:
            logger.warning(
                "dbt Cloud: environment %s has no successful run among its selected job(s) "
                "this run; keeping previously synced definitions.",
                environment_id,
            )
        else:
            logger.info(
                "dbt Cloud: environment %s has no successful run among its selected job(s); "
                "no definition documents will be synced for it.",
                environment_id,
            )
        if prior_env_state:
            return [], dict(prior_env_state)
        return [], {"manifest_run_id": None, "node_ids": []}

    if not is_full and best_run["id"] == stored_run_id:
        return [], dict(prior_env_state)

    manifest, catalog = _fetch_manifest_and_catalog(session, config, best_run)
    rows = list(_manifest_to_rows(config, environment_id, manifest, catalog))
    new_node_ids = {row["id"] for row in rows}

    if not new_node_ids and stored_node_ids:
        logger.warning(
            "dbt Cloud: manifest for environment %s's run %s yielded 0 in-scope definition "
            "rows but %d were known; skipping tombstones and keeping prior definitions.",
            environment_id,
            best_run["id"],
            len(stored_node_ids),
        )
        return [], dict(prior_env_state)

    tombstones = [
        {"id": node_id, "_deleted": True} for node_id in sorted(stored_node_ids - new_node_ids)
    ]
    return rows + tombstones, {"manifest_run_id": best_run["id"], "node_ids": sorted(new_node_ids)}


# ---------------------------------------------------------------------------
# Run outcomes
# ---------------------------------------------------------------------------


def _fetch_run_steps(session: Any, config: _Config, run_id: int) -> list[dict]:
    """Fetch a run's steps via ``include_related=run_steps`` (confirmed valid
    against the dbt Cloud OpenAPI v2 spec's run-detail endpoint).
    """
    url = f"{config.base_url}{_API_BASE_PATH}/accounts/{config.account_id}/runs/{run_id}/"
    payload = _get_json(session, url, params={"include_related": "run_steps"})
    return (payload.get("data") or {}).get("run_steps") or []


def _is_docs_generate_step(step: dict) -> bool:
    """True if a run step's name looks like a docs-generate step.

    Step names are free text (the configured dbt command for that step),
    not a documented enum, so this is a best-effort case-insensitive
    substring match rather than a guaranteed-stable identifier -- flagged
    as UNVERIFIED in the connector's research report. Confirmed separately
    (via a real dbt-core run during this connector's research) that a docs
    -generate step's own run_results.json reports "success" for every
    node regardless of an earlier step's real failures, since it only
    recompiles -- this is exactly why it must be excluded from the merge.
    """
    name = (step.get("name") or "").lower()
    return "docs generate" in name or "generate docs" in name


def _fetch_run_results(session: Any, config: _Config, run_id: int, steps: list[dict]) -> list[dict]:
    """Fetch and merge run_results.json across every step that executed
    nodes, skipping the docs-generate step (whose own run_results.json
    reflects compilation, not execution). A step with no run_results.json
    (e.g. it failed before producing one) is tolerated.
    """
    max_bytes = config.max_artifact_mb * 1024 * 1024
    base = f"{config.base_url}{_API_BASE_PATH}/accounts/{config.account_id}/runs/{run_id}/artifacts"
    merged: dict[str, dict] = {}
    for step in steps:
        if _is_docs_generate_step(step):
            continue
        index = step.get("index")
        if index is None:
            continue
        payload = _get_artifact(
            session, f"{base}/run_results.json", {"step": index}, max_bytes, optional=True
        )
        if not payload:
            continue
        for result in payload.get("results") or []:
            unique_id = result.get("unique_id")
            if unique_id:
                merged[unique_id] = result
    return list(merged.values())


def _run_doc_id(account_id: int, run_id: int) -> str:
    return f"dbt-cloud:{account_id}:run:{run_id}"


def _run_tombstone(account_id: int, run_id: int) -> dict:
    return {"id": _run_doc_id(account_id, run_id), "_deleted": True}


def _run_to_row(session: Any, config: _Config, job: dict, run: dict) -> dict:
    run_id = run["id"]
    status_label = _STATUS_LABELS.get(run.get("status"), "Unknown")
    job_label = job.get("name") or job.get("id")

    lines = [
        f"Job: {job_label} (id {job.get('id')})",
        f"Environment: {run.get('environment_id')}",
        f"Project: {run.get('project_id')}",
        f"Status: {status_label}",
    ]
    git_branch = run.get("git_branch")
    git_sha = run.get("git_sha")
    if git_branch or git_sha:
        lines.append(f"Git: {git_branch or '?'} @ {git_sha or '?'}")
    finished_at = run.get("finished_at")
    if finished_at:
        lines.append(f"Finished at: {finished_at}")
    status_message = (run.get("status_message") or "").strip()
    if status_message:
        lines.append(f"Status message: {status_message}")

    steps = _fetch_run_steps(session, config, run_id)
    results = _fetch_run_results(session, config, run_id, steps)

    counts: dict[str, int] = {}
    problem_nodes = []
    for result in results:
        status = result.get("status") or "unknown"
        counts[status] = counts.get(status, 0) + 1
        if status in ("fail", "error", "warn"):
            message = (result.get("message") or "").strip()
            if len(message) > _MESSAGE_TRIM:
                message = message[:_MESSAGE_TRIM] + "…"
            problem_nodes.append((result.get("unique_id") or "?", status, message))

    if counts:
        lines.append("Result counts: " + ", ".join(f"{k}={v}" for k, v in sorted(counts.items())))

    if problem_nodes:
        shown = problem_nodes[:_FAILING_NODE_CAP]
        lines.append(
            "Failing/erroring/warning nodes:\n"
            + "\n".join(f"- {uid} [{status}]: {msg}" for uid, status, msg in shown)
        )
        if len(problem_nodes) > _FAILING_NODE_CAP:
            lines.append(f"(+{len(problem_nodes) - _FAILING_NODE_CAP} more)")

    return {
        "id": _run_doc_id(config.account_id, run_id),
        "title": f'Run {run_id} of job "{job_label}" — {status_label}',
        "content": "\n\n".join(lines),
        "_deleted": False,
    }


def _sync_job_run_outcomes(
    session: Any,
    config: _Config,
    job: dict,
    finished_runs: list[dict],
    prior_job_state: dict,
    is_full: bool,
) -> tuple[list[dict], dict]:
    """Sync one job's run-outcome documents: a sliding window of the newest
    ``max_runs_per_job`` finished runs.

    ``finished_runs`` is this pass's scan result for the job (full: the
    complete fresh set, authoritative; incremental: only runs at/after the
    stored cursor -- a stored run not re-observed is assumed to still be
    valid unless the window slides past it). Runs new to the window are
    fetched and rendered; a run already stored is never re-fetched (a
    finished run's outcome never changes). Runs that fall out of the
    window, or that vanished from a full pass's fresh listing entirely
    (deleted, or aged past the 365-day retention window), are tombstoned.

    Guard: if a full pass's run listing comes back with zero finished runs
    while some were previously known, that's treated as a probable
    transient failure, not "every run was deleted" -- nothing is
    tombstoned and the prior state is kept as-is.
    """
    stored_runs: dict[str, str] = dict(prior_job_state.get("runs", {}))

    if not config.include_run_outcomes:
        tombstones = [_run_tombstone(config.account_id, int(rid)) for rid in stored_runs]
        return tombstones, {"cursor": None, "runs": {}}

    if is_full and not finished_runs and stored_runs:
        logger.warning(
            "dbt Cloud: full run listing for job %s returned 0 finished runs but %d were "
            "known; skipping run-outcome forget-on-delete for this job this run.",
            job["id"],
            len(stored_runs),
        )
        return [], dict(prior_job_state)

    candidates = {str(run["id"]): run for run in finished_runs}
    # A full pass's fresh listing is authoritative: a stored run absent from
    # it is genuinely gone. An incremental scan is partial (only >= cursor),
    # so a stored run it didn't re-observe is assumed to still exist unless
    # the window slides past it below.
    all_ids = set(candidates) if is_full else (set(candidates) | set(stored_runs))

    def _finished_at_of(run_id: str) -> datetime:
        source = candidates[run_id]["finished_at"] if run_id in candidates else stored_runs[run_id]
        return _parse_dt(source)

    window_ids = sorted(all_ids, key=_finished_at_of, reverse=True)[: config.max_runs_per_job]
    window_set = set(window_ids)

    rows = []
    new_runs: dict[str, str] = {}
    for run_id in window_ids:
        if run_id in candidates:
            new_runs[run_id] = candidates[run_id]["finished_at"]
            if run_id not in stored_runs:
                rows.append(_run_to_row(session, config, job, candidates[run_id]))
        else:
            new_runs[run_id] = stored_runs[run_id]

    dropped_ids = sorted(set(stored_runs) - window_set, key=int)
    tombstones = [_run_tombstone(config.account_id, int(rid)) for rid in dropped_ids]

    newest_finished_at = prior_job_state.get("cursor")
    for run in candidates.values():
        is_newer = newest_finished_at is None or _parse_dt(run["finished_at"]) > _parse_dt(
            newest_finished_at
        )
        if is_newer:
            newest_finished_at = run["finished_at"]

    return rows + tombstones, {"cursor": newest_finished_at, "runs": new_runs}


# ---------------------------------------------------------------------------
# Sync orchestration
# ---------------------------------------------------------------------------


def _reconcile_vanished_jobs_and_envs(state: dict, config: _Config, jobs: list[dict]) -> list[dict]:
    """On a full pass, tombstone everything stored for a job or environment
    no longer in the resolved selection, and drop it from state.

    Guarded against an empty resolved job set: if state has jobs but none
    were resolved this pass, that's treated as a probable transient
    failure (every job temporarily inaccessible, a misconfigured filter,
    ...) rather than "every job was removed" -- nothing is tombstoned and
    state is left untouched. This step makes no API calls itself, so there
    is no partial-failure window within it; it only ever runs once `jobs`
    has already been resolved successfully.
    """
    stored_jobs = state.setdefault("jobs", {})
    stored_envs = state.setdefault("envs", {})

    current_job_ids = {str(job["id"]) for job in jobs}
    current_env_ids = {str(job.get("environment_id")) for job in jobs}

    if stored_jobs and not current_job_ids:
        logger.warning(
            "dbt Cloud: no jobs resolved this run but %d were known; skipping job/environment "
            "forget-on-delete this run.",
            len(stored_jobs),
        )
        return []

    rows: list[dict] = []
    for job_id in sorted(set(stored_jobs) - current_job_ids, key=int):
        job_state = stored_jobs.pop(job_id)
        for run_id in job_state.get("runs", {}):
            rows.append(_run_tombstone(config.account_id, int(run_id)))

    for environment_id in sorted(set(stored_envs) - current_env_ids, key=str):
        env_state = stored_envs.pop(environment_id)
        for node_id in env_state.get("node_ids", []):
            rows.append({"id": node_id, "_deleted": True})

    return rows


def _iter_rows(session: Any, config: _Config, state: dict):
    """Yield one row per in-scope definition/run, plus tombstones for
    removed definitions/runs/jobs/environments.

    Runs a FULL reconciliation pass when ``state`` is empty, the resolved
    configuration changed since last time, or due by the
    ``full_sync_every`` counter; otherwise an INCREMENTAL pass.

    Decoupled from dlt's resource-state machinery (which needs an active
    pipeline context) so it is directly unit-testable with a fake session
    and a plain dict standing in for dlt's resource state. Each
    environment's and each job's new state is written into
    ``state["envs"][...]``/``state["jobs"][...]`` only once that scope's
    sync has returned successfully -- an exception partway through
    propagates immediately, before that scope's (or any later scope's)
    state is touched. dlt's own pipeline-state manager additionally rolls
    back the entire state dict to its pre-run value if any exception
    reaches it (``dlt.pipeline.pipeline.Pipeline.managed_state``, the same
    guarantee verified for the Bitbucket connector), so this function's own
    discipline is a second, independent safety layer on top of that
    guarantee rather than the only thing protecting against it.
    """
    config_hash = _config_hash(config)
    prior_hash = state.get("config_hash")
    runs_since_full = state.get("runs_since_full", 0)
    is_full = (
        not state or prior_hash != config_hash or runs_since_full + 1 >= config.full_sync_every
    )

    jobs = _resolve_jobs(session, config)

    total = 0
    if is_full:
        vanished_rows = _reconcile_vanished_jobs_and_envs(state, config, jobs)
        yield from vanished_rows
        total += len(vanished_rows)

    prior_jobs_state = dict(state.get("jobs", {}))
    prior_envs_state = dict(state.get("envs", {}))

    job_scans: dict[int, tuple[list[dict], dict | None]] = {}
    for job in jobs:
        prior_job_state = prior_jobs_state.get(str(job["id"]), {})
        cursor = None if is_full else prior_job_state.get("cursor")
        job_scans[job["id"]] = _scan_job_runs(session, config, job, cursor)

    jobs_by_env: dict[Any, list[dict]] = {}
    for job in jobs:
        jobs_by_env.setdefault(job.get("environment_id"), []).append(job)

    for environment_id, env_jobs in jobs_by_env.items():
        candidates = [job_scans[job["id"]][1] for job in env_jobs]
        prior_env_state = prior_envs_state.get(str(environment_id), {})
        rows, new_env_state = _sync_environment_definitions(
            session, config, environment_id, candidates, prior_env_state, is_full
        )
        yield from rows
        total += len(rows)
        state.setdefault("envs", {})[str(environment_id)] = new_env_state

    for job in jobs:
        prior_job_state = prior_jobs_state.get(str(job["id"]), {})
        finished_runs, _latest_success = job_scans[job["id"]]
        rows, new_job_state = _sync_job_run_outcomes(
            session, config, job, finished_runs, prior_job_state, is_full
        )
        yield from rows
        total += len(rows)
        state.setdefault("jobs", {})[str(job["id"])] = new_job_state

    state["config_hash"] = config_hash
    state["runs_since_full"] = 0 if is_full else runs_since_full + 1
    logger.info(
        "dbt Cloud: %s sync yielded %d document(s) across %d job(s).",
        "full" if is_full else "incremental",
        total,
        len(jobs),
    )
