"""Apache Airflow connector for cognee: a dlt resource that syncs DAG metadata into memory.

It reads the Airflow 3.x public REST API (``/api/v2``) and yields one document per
DAG, handed to ``cognee.remember``::

    await cognee.remember(
        airflow_source(base_url="http://localhost:8080", username="admin", password="..."),
        dataset_name="airflow",
        max_rows_per_table=0,
    )

Each DAG document writes its structure out as plain statements ("extract_orders
runs before validate_orders", "transform_revenue writes dataset ...") so cognify
extracts them as graph relations. Task inlets/outlets come from Airflow Assets
(``/api/v2/assets``); the tasks endpoint does not return them.

Sync is a full snapshot (``write_disposition="replace"``):

* a DAG removed from its bundle turns stale, is excluded, and cognee's orphan
  cleanup forgets it on the next sync; a paused DAG still exists and is kept;
* the structure document carries no volatile fields (next run, parse time), so
  an unchanged DAG keeps its content hash and is not re-cognified;
* any failed request aborts the run before anything is loaded, so a partial
  listing can never make cognee forget live DAGs.

Recent runs are optional (``include_runs``) and go into a separate document per
DAG, bounded by a time window and a run count. That is a recent-history window,
not an incremental sync.
"""

import fnmatch
import time
from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urlparse

import requests
from cognee.shared.logging_utils import get_logger
from cognee.tasks.ingestion import dlt_utils
from cognee.tasks.ingestion.dlt_utils import DOCUMENT_SOURCE_ATTR

logger = get_logger("airflow_connector")

AIRFLOW_SOURCE_TAG = "airflow"
# Default dlt resource / staging-table name.
AIRFLOW_TABLE_NAME = "airflow_dags"
# Airflow's default maximum page size for list endpoints.
PAGE_SIZE = 100
# (connect, read) seconds.
_TIMEOUT = (10, 30)
# Attempts per request on 429/5xx or network errors.
_RETRIES = 3
# Asset URI schemes whose path is <database>/<schema>/<table>; rendered as the
# dotted table name that catalogs such as DataHub use for the same table.
_SQL_SCHEMES = {"postgres", "postgresql", "mysql", "mssql", "redshift", "trino"}


class AirflowSourceError(RuntimeError):
    """Base class of the errors this source raises."""


class AirflowAuthError(AirflowSourceError):
    """Airflow rejected the credentials (HTTP 401/403)."""


class AirflowAPIError(AirflowSourceError):
    """Airflow answered with an error, or could not be reached."""


class AirflowClient:
    """Minimal ``/api/v2`` client: bearer auth, re-auth on 401, retries, paging."""

    def __init__(
        self,
        base_url: str,
        *,
        token: str | None = None,
        username: str | None = None,
        password: str | None = None,
        session: requests.Session | None = None,
    ):
        if not base_url:
            raise ValueError("airflow_source needs a base_url")
        if not token and not (username and password):
            raise ValueError("airflow_source needs a token, or a username and password")
        self.base_url = base_url.rstrip("/")
        self._username = username
        self._password = password
        self._token = token
        self._session = session or requests.Session()
        self._secrets = [s for s in (token, password) if s]

    def _login(self) -> None:
        # Exchanges username/password for a JWT (Airflow 3 /auth/token).
        response = self._send(
            "POST",
            "/auth/token",
            json={
                "username": self._username,
                "password": self._password,
            },
            authenticated=False,
        )
        if response.status_code in (401, 403):
            raise AirflowAuthError("Airflow rejected the username or password")
        self._raise_for_status(response, "/auth/token")
        self._token = response.json().get("access_token")
        if not self._token:
            raise AirflowAPIError("Airflow /auth/token returned no access_token")
        self._secrets.append(self._token)

    def get(self, path: str, params: dict | list | None = None) -> dict:
        if not self._token:
            self._login()
        response = self._send("GET", path, params=params)
        if response.status_code == 401 and self._username and self._password:
            # JWTs expire (24h by default): log in again once and retry.
            self._login()
            response = self._send("GET", path, params=params)
        if response.status_code in (401, 403):
            raise AirflowAuthError(f"Airflow refused {path} (HTTP {response.status_code})")
        self._raise_for_status(response, path)
        body = response.json()
        if not isinstance(body, dict):
            raise AirflowAPIError(f"Airflow sent a non-object body for {path}")
        return body

    def paginate(self, path: str, key: str, params: list | None = None) -> list[dict]:
        """Read every page of a limit/offset list endpoint (supported by all 3.x)."""
        items: list[dict] = []
        offset = 0
        while True:
            page = self.get(path, [*(params or []), ("limit", PAGE_SIZE), ("offset", offset)])
            batch = page.get(key) or []
            items.extend(batch)
            offset += len(batch)
            total = page.get("total_entries")
            if not batch or (isinstance(total, int) and offset >= total):
                return items

    def _send(self, method: str, path: str, authenticated: bool = True, **kwargs):
        headers = {"Accept": "application/json"}
        if authenticated and self._token:
            headers["Authorization"] = f"Bearer {self._token}"
        for attempt in range(_RETRIES):
            try:
                response = self._session.request(
                    method, self.base_url + path, headers=headers, timeout=_TIMEOUT, **kwargs
                )
            except requests.RequestException as exc:
                if attempt == _RETRIES - 1:
                    raise AirflowAPIError(
                        f"Airflow is unreachable at {self.base_url}: {self._redact(str(exc))}"
                    ) from None
            else:
                if response.status_code not in (429, 500, 502, 503, 504) or attempt == _RETRIES - 1:
                    return response
            time.sleep(2**attempt)
        raise AssertionError("unreachable")

    def _raise_for_status(self, response, path: str) -> None:
        if response.status_code >= 400:
            detail = self._redact(response.text[:200])
            raise AirflowAPIError(
                f"Airflow {path} failed with HTTP {response.status_code}: {detail}"
            )

    def _redact(self, text: str) -> str:
        for secret in self._secrets:
            text = text.replace(secret, "***")
        return text


# ---------------------------------------------------------------------------
# Listing
# ---------------------------------------------------------------------------


def _list_dags(client: AirflowClient, dag_ids, dag_id_pattern, tags) -> list[dict]:
    params: list[tuple[str, Any]] = [("exclude_stale", "true"), ("order_by", "dag_id")]
    for tag in tags or []:
        params.append(("tags", tag))
    if tags:
        params.append(("tags_match_mode", "any"))
    dags = client.paginate("/api/v2/dags", "dags", params)
    if dag_id_pattern:
        dags = [d for d in dags if fnmatch.fnmatchcase(d["dag_id"], dag_id_pattern)]
    if dag_ids:
        wanted = set(dag_ids)
        dags = [d for d in dags if d["dag_id"] in wanted]
    # Belt and braces: never ingest a stale DAG even if the server ignored the filter.
    return [d for d in dags if not d.get("is_stale")]


def _task_assets(client: AirflowClient) -> dict[tuple[str, str], dict[str, list[str]]]:
    """Map (dag_id, task_id) to the asset URIs that task reads and writes."""
    io: dict[tuple[str, str], dict[str, list[str]]] = {}
    for asset in client.paginate("/api/v2/assets", "assets"):
        uri = asset.get("uri")
        if not uri:
            continue
        for direction, key in (("writes", "producing_tasks"), ("reads", "consuming_tasks")):
            for ref in asset.get(key) or []:
                slot = io.setdefault((ref["dag_id"], ref["task_id"]), {"reads": [], "writes": []})
                slot[direction].append(uri)
    return io


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def asset_table_name(uri: str) -> str | None:
    """``postgres://host:5432/db/schema/table`` -> ``db.schema.table`` (SQL schemes only)."""
    parsed = urlparse(uri)
    if parsed.scheme.lower() not in _SQL_SCHEMES:
        return None
    parts = [p for p in parsed.path.split("/") if p]
    return ".".join(parts) if len(parts) == 3 else None


def _asset_phrase(uri: str) -> str:
    table = asset_table_name(uri)
    return f"dataset {table} (asset {uri})" if table else f"asset {uri}"


def _clean(text: Any) -> str:
    return " ".join(str(text).split()) if text else ""


def render_dag(dag: dict, details: dict, tasks: list[dict], io: dict) -> str:
    dag_id = dag["dag_id"]
    owners = ", ".join(sorted(dag.get("owners") or [])) or "unknown"
    tags = ", ".join(sorted(t["name"] for t in dag.get("tags") or [])) or "none"
    schedule = dag.get("timetable_description") or "not scheduled (manual or asset-triggered)"
    lines = [
        f"Airflow DAG {dag_id}",
        f"Description: {_clean(dag.get('description')) or 'none'}",
        f"Owners: {owners}",
        f"Tags: {tags}",
        f"Schedule: {schedule}",
        f"State: {'paused' if dag.get('is_paused') else 'active'}",
        f"Defined in: {dag.get('relative_fileloc') or dag.get('fileloc') or 'unknown'}",
    ]
    if details.get("doc_md"):
        lines += ["", "Documentation:", details["doc_md"].strip()]
    lines += ["", f"Tasks of DAG {dag_id}:"]
    for task in sorted(tasks, key=lambda t: t["task_id"]):
        task_id = task["task_id"]
        lines.append(
            f"- Task {task_id} belongs to DAG {dag_id}; operator {task.get('operator_name')}, "
            f"owner {task.get('owner') or owners}, retries {int(task.get('retries') or 0)}, "
            f"trigger rule {task.get('trigger_rule')}."
        )
        if task.get("doc_md"):
            lines.append(f"  {task_id}: {_clean(task['doc_md'])}")
        for downstream in sorted(task.get("downstream_task_ids") or []):
            lines.append(f"  {task_id} runs before {downstream}.")
        task_io = io.get((dag_id, task_id), {})
        for uri in sorted(set(task_io.get("reads", []))):
            lines.append(f"  {task_id} reads {_asset_phrase(uri)}.")
        for uri in sorted(set(task_io.get("writes", []))):
            lines.append(f"  {task_id} writes {_asset_phrase(uri)}.")
    return "\n".join(lines)


def render_runs(
    dag_id: str, runs: list[dict], tasks_by_run: dict[str, list[dict]], window_days: int, io: dict
) -> str:
    lines = [f"Recent runs of Airflow DAG {dag_id} (last {window_days} days, newest first):"]
    if not runs:
        lines.append("No runs in this window.")
    for run in runs:
        run_id = run["dag_run_id"]
        lines.append(
            f"- Run {run_id} of DAG {dag_id} ({run.get('run_type')}) finished in state "
            f"{run.get('state')}; started {run.get('start_date')}, ended {run.get('end_date')}."
        )
        for ti in sorted(tasks_by_run.get(run_id, []), key=lambda t: t["task_id"]):
            state = ti.get("state")
            writes = io.get((dag_id, ti["task_id"]), {}).get("writes", [])
            note = "".join(f" It did not update {_asset_phrase(u)}." for u in sorted(set(writes)))
            if state == "failed":
                lines.append(
                    f"  In run {run_id}, task {ti['task_id']} failed "
                    f"(try {ti.get('try_number')}).{note}"
                )
            elif state == "upstream_failed":
                lines.append(
                    f"  In run {run_id}, task {ti['task_id']} did not run because an upstream "
                    f"task failed.{note}"
                )
    return "\n".join(lines)


def _dag_url(base_url: str, dag_id: str) -> str:
    return f"{base_url}/dags/{dag_id}"


def iter_rows(
    client: AirflowClient,
    *,
    dag_ids: list[str] | None = None,
    dag_id_pattern: str | None = None,
    tags: list[str] | None = None,
    include_runs: bool = False,
    runs_window_days: int = 7,
    max_runs_per_dag: int = 5,
    check_active: Callable[[], None] | None = None,
) -> Iterator[dict]:
    """Yield one structure row per DAG (plus one runs row each when ``include_runs``).

    Everything is fetched before the first row is yielded, so a failure part-way
    through raises before dlt loads anything and staging keeps the last snapshot.
    """

    def guard():
        if check_active is not None:
            check_active()

    guard()
    dags = _list_dags(client, dag_ids, dag_id_pattern, tags)
    guard()
    io = _task_assets(client)
    since = (datetime.now(UTC) - timedelta(days=runs_window_days)).isoformat()
    rows: list[dict] = []
    for dag in dags:
        dag_id = dag["dag_id"]
        guard()
        details = client.get(f"/api/v2/dags/{dag_id}/details")
        tasks = client.get(f"/api/v2/dags/{dag_id}/tasks").get("tasks") or []
        rows.append(
            {
                "id": f"airflow:dag:{dag_id}",
                "title": f"Airflow DAG {dag_id}",
                "content": render_dag(dag, details, tasks, io),
                "url": _dag_url(client.base_url, dag_id),
            }
        )
        if include_runs:
            runs = (
                client.get(
                    f"/api/v2/dags/{dag_id}/dagRuns",
                    [
                        ("order_by", "-run_after"),
                        ("limit", max_runs_per_dag),
                        ("end_date_gte", since),
                    ],
                ).get("dag_runs")
                or []
            )
            tasks_by_run = {
                run["dag_run_id"]: client.paginate(
                    f"/api/v2/dags/{dag_id}/dagRuns/{run['dag_run_id']}/taskInstances",
                    "task_instances",
                )
                for run in runs
            }
            rows.append(
                {
                    "id": f"airflow:runs:{dag_id}",
                    "title": f"Recent runs of Airflow DAG {dag_id}",
                    "content": render_runs(dag_id, runs, tasks_by_run, runs_window_days, io),
                    "url": f"{_dag_url(client.base_url, dag_id)}/runs",
                }
            )
    logger.info("Airflow: synced %d DAG(s).", len(dags))
    yield from rows


# ---------------------------------------------------------------------------
# Public factory
# ---------------------------------------------------------------------------


def airflow_source(
    *,
    base_url: str,
    token: str | None = None,
    username: str | None = None,
    password: str | None = None,
    dag_ids: list[str] | None = None,
    dag_id_pattern: str | None = None,
    tags: list[str] | None = None,
    include_runs: bool = False,
    runs_window_days: int = 7,
    max_runs_per_dag: int = 5,
    resource_name: str = AIRFLOW_TABLE_NAME,
    session: requests.Session | None = None,
    check_active: Callable[[], None] | None = None,
):
    """Return a ``dlt`` resource that yields Airflow DAG documents for ``cognee.remember``.

    Args:
        base_url: Airflow API server, e.g. ``http://localhost:8080`` (no ``/api/v2``).
        token: A pre-issued bearer token (JWT or managed-deployment token).
        username, password: Exchanged for a JWT via ``POST /auth/token``; also used
            to log in again when the token expires.
        dag_ids: Only sync these DAGs.
        dag_id_pattern: Only sync DAGs whose id matches this glob (``retail_*``).
        tags: Only sync DAGs carrying any of these tags.
        include_runs: Also sync a short summary of recent runs per DAG (off by default).
        runs_window_days, max_runs_per_dag: Bounds of that run summary.
        resource_name: Staging-table name; keep it fixed per Airflow deployment, since
            it scopes forget-on-delete.
        session: Pre-built ``requests.Session``, mainly a test injection point.
        check_active: Optional host authorization checkpoint.

    Returns:
        A ``dlt`` resource with ``primary_key="id"`` and ``write_disposition="replace"``.
        Pass ``max_rows_per_table=0`` to ``remember`` so a capped read can never look
        like deleted DAGs.
    """
    import dlt

    if getattr(dlt_utils, "DOCUMENT_SYNC_VERSION", 0) < 1:
        raise RuntimeError("The Airflow connector requires cognee>=1.6.2 (table-scoped cleanup).")
    if not isinstance(resource_name, str) or not resource_name.strip():
        raise ValueError("resource_name must be a non-empty string")
    if runs_window_days < 1 or max_runs_per_dag < 1:
        raise ValueError("runs_window_days and max_runs_per_dag must be at least 1")

    # Built here so missing credentials fail at construction and the closure holds no raw args.
    client = AirflowClient(
        base_url, token=token, username=username, password=password, session=session
    )

    @dlt.resource(name=resource_name, primary_key="id", write_disposition="replace")
    def airflow_dags():
        rows = iter_rows(
            client,
            dag_ids=dag_ids,
            dag_id_pattern=dag_id_pattern,
            tags=tags,
            include_runs=include_runs,
            runs_window_days=runs_window_days,
            max_runs_per_dag=max_runs_per_dag,
            check_active=check_active,
        )
        yield from dlt_utils.guarded_rows(rows, check_active)

    resource = airflow_dags()
    setattr(resource, DOCUMENT_SOURCE_ATTR, AIRFLOW_SOURCE_TAG)
    setattr(resource, dlt_utils.PIPELINE_SCOPE_ATTR, resource_name)
    return resource
