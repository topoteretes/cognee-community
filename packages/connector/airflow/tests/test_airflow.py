"""Tests for the Airflow connector.

* Unit tests against a fake ``/api/v2`` session: rendering, asset mapping,
  pagination, auth and re-auth, scoping, error handling.
* dlt-pipeline tests (temp sqlite destination) for the sync scenarios: first
  sync, edits re-synced, stale DAG forgotten, failed fetch keeps the last
  snapshot, no truncation.
"""

import copy
import json
from urllib.parse import parse_qsl, urlparse

import pytest

from cognee_community_connector_airflow.airflow import (
    PAGE_SIZE,
    AirflowAPIError,
    AirflowAuthError,
    AirflowClient,
    airflow_source,
    asset_table_name,
    iter_rows,
    render_dag,
)

BASE = "http://airflow.test"
WAREHOUSE = "postgres://postgres:5432/retail_demo"


class FakeResponse:
    def __init__(self, status: int, body):
        self.status_code = status
        self._body = body
        self.text = json.dumps(body)

    def json(self):
        return self._body


class FakeAirflow:
    """In-memory stand-in for an Airflow 3 API server, used as a requests.Session."""

    def __init__(self, dags=None, tasks=None, assets=None, runs=None, tis=None):
        self.dags = dags if dags is not None else [_dag("retail_analytics_pipeline")]
        self.tasks = (
            tasks if tasks is not None else {"retail_analytics_pipeline": _pipeline_tasks()}
        )
        self.assets = assets if assets is not None else _pipeline_assets()
        self.runs = runs or {}
        self.tis = tis or {}
        self.valid_tokens = {"good-token"}
        self.password = "secret"
        self.calls: list[tuple[str, str, list]] = []
        self.fail_paths: set[str] = set()

    def request(self, method, url, headers=None, params=None, json=None, timeout=None):
        parsed = urlparse(url)
        path = parsed.path
        params = list(params or [])
        self.calls.append((method, path, params))
        if path == "/auth/token":
            if json == {"username": "admin", "password": self.password}:
                token = f"jwt-{len(self.valid_tokens)}"
                self.valid_tokens.add(token)
                return FakeResponse(201, {"access_token": token})
            return FakeResponse(401, {"detail": "Invalid credentials"})
        auth = (headers or {}).get("Authorization", "")
        if auth.removeprefix("Bearer ") not in self.valid_tokens:
            return FakeResponse(401, {"detail": "Not authenticated"})
        if path in self.fail_paths:
            return FakeResponse(500, {"detail": "boom"})
        query = dict(params)
        if path == "/api/v2/dags":
            dags = self.dags
            if query.get("exclude_stale") == "true":
                dags = [d for d in dags if not d.get("is_stale")]
            return _page(dags, "dags", query)
        if path == "/api/v2/assets":
            return _page(self.assets, "assets", query)
        parts = path.split("/")
        # /api/v2/dags/{dag_id}/...
        dag_id = parts[4]
        if path.endswith("/details"):
            return FakeResponse(200, {"doc_md": f"Docs for {dag_id}."})
        if path.endswith("/tasks"):
            tasks = self.tasks.get(dag_id, [])
            return FakeResponse(200, {"tasks": tasks, "total_entries": len(tasks)})
        if path.endswith("/dagRuns"):
            runs = self.runs.get(dag_id, [])
            return FakeResponse(200, {"dag_runs": runs, "total_entries": len(runs)})
        if path.endswith("/taskInstances"):
            tis = self.tis.get(parts[6], [])
            return _page(tis, "task_instances", query)
        return FakeResponse(404, {"detail": "not found"})


def _page(items, key, query):
    offset, limit = int(query.get("offset", 0)), int(query.get("limit", PAGE_SIZE))
    return FakeResponse(200, {key: items[offset : offset + limit], "total_entries": len(items)})


def _dag(dag_id, **overrides):
    dag = {
        "dag_id": dag_id,
        "description": "Builds daily revenue.",
        "owners": ["revenue-analytics"],
        "tags": [{"name": "finance"}, {"name": "retail_demo"}],
        "timetable_description": "At 02:00",
        "is_paused": False,
        "is_stale": False,
        "relative_fileloc": f"{dag_id}.py",
        # Volatile fields the structure document must ignore.
        "next_dagrun_run_after": "2026-10-28T02:00:00Z",
        "last_parsed_time": "2026-10-27T12:00:00Z",
    }
    dag.update(overrides)
    return dag


def _task(task_id, downstream=(), operator="SQLExecuteQueryOperator"):
    return {
        "task_id": task_id,
        "operator_name": operator,
        "owner": "revenue-analytics",
        "retries": 0,
        "trigger_rule": "all_success",
        "downstream_task_ids": list(downstream),
        "doc_md": None,
    }


def _pipeline_tasks():
    return [
        _task("extract_orders", ["validate_orders"]),
        _task("validate_orders", ["transform_revenue"], operator="@task"),
        _task("transform_revenue", ["publish_daily_revenue"]),
        _task("publish_daily_revenue"),
    ]


def _pipeline_assets():
    ref = lambda task_id: {"dag_id": "retail_analytics_pipeline", "task_id": task_id}  # noqa: E731
    return [
        {
            "uri": f"{WAREHOUSE}/raw/orders",
            "producing_tasks": [],
            "consuming_tasks": [ref("extract_orders"), ref("transform_revenue")],
        },
        {
            "uri": f"{WAREHOUSE}/analytics/daily_revenue",
            "producing_tasks": [ref("transform_revenue")],
            "consuming_tasks": [ref("publish_daily_revenue")],
        },
    ]


def _client(fake, **kwargs):
    kwargs = kwargs or {"token": "good-token"}
    return AirflowClient(BASE, session=fake, **kwargs)


def _rows(fake, **kwargs):
    return list(iter_rows(_client(fake), **kwargs))


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def test_asset_table_name_maps_sql_uris_only():
    assert (
        asset_table_name(f"{WAREHOUSE}/analytics/daily_revenue")
        == "retail_demo.analytics.daily_revenue"
    )
    assert asset_table_name("s3://bucket/key.csv") is None
    assert asset_table_name("postgres://host/db_only") is None


def test_dag_document_states_dependencies_ownership_and_lineage():
    (row,) = _rows(FakeAirflow())
    text = row["content"]
    assert row["id"] == "airflow:dag:retail_analytics_pipeline"
    assert row["url"] == f"{BASE}/dags/retail_analytics_pipeline"
    assert "Owners: revenue-analytics" in text
    assert "Tags: finance, retail_demo" in text
    assert "Schedule: At 02:00" in text
    assert "State: active" in text
    assert "extract_orders runs before validate_orders." in text
    assert "validate_orders runs before transform_revenue." in text
    assert (
        "transform_revenue writes dataset retail_demo.analytics.daily_revenue "
        f"(asset {WAREHOUSE}/analytics/daily_revenue)." in text
    )
    assert "transform_revenue reads dataset retail_demo.raw.orders" in text
    assert "Docs for retail_analytics_pipeline." in text


def test_volatile_fields_do_not_change_the_document():
    first = render_dag(_dag("d"), {}, [_task("t")], {})
    later = render_dag(
        _dag("d", next_dagrun_run_after="2026-12-01", last_parsed_time="2026-12-01"),
        {},
        [_task("t")],
        {},
    )
    assert first == later


def test_paused_dag_is_kept_and_marked_paused():
    fake = FakeAirflow(dags=[_dag("retail_analytics_pipeline", is_paused=True)])
    (row,) = _rows(fake)
    assert "State: paused" in row["content"]


def test_stale_dag_is_excluded_even_if_server_returns_it():
    fake = FakeAirflow(dags=[_dag("live"), _dag("gone", is_stale=True)], tasks={})
    fake_request = fake.request

    def ignore_filter(method, url, params=None, **kw):
        params = [p for p in (params or []) if p[0] != "exclude_stale"]
        return fake_request(method, url, params=params, **kw)

    fake.request = ignore_filter
    assert [r["id"] for r in _rows(fake)] == ["airflow:dag:live"]


# ---------------------------------------------------------------------------
# Runs
# ---------------------------------------------------------------------------


def test_runs_are_a_separate_document_with_failures_and_skipped_outputs():
    fake = FakeAirflow(
        runs={
            "retail_analytics_pipeline": [
                {
                    "dag_run_id": "lab_failure_1",
                    "run_type": "manual",
                    "state": "failed",
                    "start_date": "2026-10-27T12:06:23Z",
                    "end_date": "2026-10-27T12:06:29Z",
                },
            ]
        },
        tis={
            "lab_failure_1": [
                {"task_id": "validate_orders", "state": "failed", "try_number": 1},
                {"task_id": "transform_revenue", "state": "upstream_failed", "try_number": 0},
                {"task_id": "extract_orders", "state": "success", "try_number": 1},
            ]
        },
    )
    structure, runs = _rows(fake, include_runs=True)
    assert runs["id"] == "airflow:runs:retail_analytics_pipeline"
    assert (
        "Run lab_failure_1 of DAG retail_analytics_pipeline (manual) finished in state failed"
        in runs["content"]
    )
    assert "task validate_orders failed (try 1)" in runs["content"]
    assert (
        "task transform_revenue did not run because an upstream task failed. "
        "It did not update dataset retail_demo.analytics.daily_revenue" in runs["content"]
    )
    assert "extract_orders" not in runs["content"]
    # Run history stays out of the structure document.
    assert "lab_failure_1" not in structure["content"]


def test_failed_task_that_writes_an_asset_says_what_it_did_not_update():
    fake = FakeAirflow(
        runs={
            "retail_analytics_pipeline": [
                {"dag_run_id": "r1", "run_type": "scheduled", "state": "failed"},
            ]
        },
        tis={"r1": [{"task_id": "transform_revenue", "state": "failed", "try_number": 2}]},
    )
    _, runs = _rows(fake, include_runs=True)
    assert (
        "task transform_revenue failed (try 2). "
        "It did not update dataset retail_demo.analytics.daily_revenue" in runs["content"]
    )


def test_runs_request_is_bounded():
    fake = FakeAirflow()
    _rows(fake, include_runs=True, runs_window_days=3, max_runs_per_dag=2)
    (params,) = [p for m, path, p in fake.calls if path.endswith("/dagRuns")]
    query = dict(params)
    assert query["limit"] == 2
    assert query["order_by"] == "-run_after"
    assert "end_date_gte" in query


# ---------------------------------------------------------------------------
# Paging, scope, auth, errors
# ---------------------------------------------------------------------------


def test_pagination_reads_every_page():
    dags = [_dag(f"dag_{i:03d}") for i in range(PAGE_SIZE * 2 + 5)]
    fake = FakeAirflow(dags=dags, tasks={}, assets=[])
    rows = _rows(fake)
    assert len(rows) == len(dags)
    offsets = [dict(p)["offset"] for m, path, p in fake.calls if path == "/api/v2/dags"]
    assert offsets == [0, PAGE_SIZE, PAGE_SIZE * 2]


def test_scope_by_ids_pattern_and_tags():
    fake = FakeAirflow(
        dags=[_dag("retail_a"), _dag("retail_b"), _dag("other")], tasks={}, assets=[]
    )
    assert [r["id"] for r in _rows(fake, dag_id_pattern="retail_*")] == [
        "airflow:dag:retail_a",
        "airflow:dag:retail_b",
    ]
    assert [r["id"] for r in _rows(fake, dag_ids=["other"])] == ["airflow:dag:other"]
    _rows(fake, tags=["finance"])
    params = [p for m, path, p in fake.calls if path == "/api/v2/dags"][-1]
    assert ("tags", "finance") in params


def test_username_password_login_and_reauth_on_expiry():
    fake = FakeAirflow()
    client = _client(fake, username="admin", password="secret")
    client.get("/api/v2/dags")
    assert [path for m, path, p in fake.calls].count("/auth/token") == 1
    fake.valid_tokens = {"good-token"}  # every issued JWT has now expired
    client.get("/api/v2/dags")
    assert [path for m, path, p in fake.calls].count("/auth/token") == 2


def test_login_token_is_redacted_from_errors():
    fake = FakeAirflow()
    client = _client(fake, username="admin", password="secret")
    client.get("/api/v2/dags")
    token = client._token
    assert client._redact(f"echo {token} and secret") == "echo *** and ***"


def test_bad_credentials_and_missing_credentials():
    fake = FakeAirflow()
    with pytest.raises(AirflowAuthError):
        _client(fake, username="admin", password="wrong").get("/api/v2/dags")
    with pytest.raises(AirflowAuthError):
        _client(fake, token="expired").get("/api/v2/dags")
    with pytest.raises(ValueError):
        AirflowClient(BASE)


def test_server_error_aborts_without_rows_and_redacts_secrets(monkeypatch):
    monkeypatch.setattr("cognee_community_connector_airflow.airflow.time.sleep", lambda s: None)
    fake = FakeAirflow()
    fake.fail_paths = {"/api/v2/dags/retail_analytics_pipeline/tasks"}
    with pytest.raises(AirflowAPIError) as err:
        _rows(fake)
    assert "good-token" not in str(err.value)


def test_source_declares_document_marker_and_scope():
    from cognee.tasks.ingestion.dlt_utils import PIPELINE_SCOPE_ATTR, document_source_tag

    resource = airflow_source(base_url=BASE, token="good-token", resource_name="airflow_prod")
    assert document_source_tag(resource) == "airflow"
    assert getattr(resource, PIPELINE_SCOPE_ATTR) == "airflow_prod"
    assert resource.write_disposition == "replace"


# ---------------------------------------------------------------------------
# dlt pipeline: full snapshot + forget-on-delete
# ---------------------------------------------------------------------------


@pytest.fixture
def dlt_mod():
    return pytest.importorskip("dlt")


def _sync(dlt, tmp_path, fake):
    db = (tmp_path / "airflow.db").as_posix()
    pipeline = dlt.pipeline(
        pipeline_name="airflow_test",
        destination=dlt.destinations.sqlalchemy(f"sqlite:///{db}"),
        dataset_name="airflow_ds",
        pipelines_dir=str(tmp_path / "state"),
    )
    pipeline.run(airflow_source(base_url=BASE, token="good-token", session=fake))
    with pipeline.sql_client() as sql:
        return {r[0]: r[1] for r in sql.execute_sql("SELECT id, content FROM airflow_dags")}


def test_first_sync_and_edit_is_reflected(dlt_mod, tmp_path):
    fake = FakeAirflow()
    first = _sync(dlt_mod, tmp_path, fake)
    assert list(first) == ["airflow:dag:retail_analytics_pipeline"]
    fake.dags = [_dag("retail_analytics_pipeline", owners=["finance-team"])]
    second = _sync(dlt_mod, tmp_path, fake)
    assert "Owners: finance-team" in second["airflow:dag:retail_analytics_pipeline"]


def test_stale_dag_drops_out_and_paused_stays(dlt_mod, tmp_path):
    fake = FakeAirflow(dags=[_dag("a"), _dag("b"), _dag("c")], tasks={}, assets=[])
    assert len(_sync(dlt_mod, tmp_path, fake)) == 3
    fake.dags = [_dag("a"), _dag("b", is_stale=True), _dag("c", is_paused=True)]
    rows = _sync(dlt_mod, tmp_path, fake)
    assert sorted(rows) == ["airflow:dag:a", "airflow:dag:c"]
    assert "State: paused" in rows["airflow:dag:c"]


def test_failed_fetch_keeps_previous_snapshot(dlt_mod, tmp_path, monkeypatch):
    monkeypatch.setattr("cognee_community_connector_airflow.airflow.time.sleep", lambda s: None)
    fake = FakeAirflow(dags=[_dag("a"), _dag("b")], tasks={}, assets=[])
    before = _sync(dlt_mod, tmp_path, fake)
    broken = copy.deepcopy(fake)
    broken.fail_paths = {"/api/v2/dags/b/details"}
    with pytest.raises(Exception):  # noqa: B017 - dlt wraps the source error
        _sync(dlt_mod, tmp_path, broken)
    assert _sync(dlt_mod, tmp_path, fake) == before


def test_large_deployments_are_not_truncated(dlt_mod, tmp_path):
    dags = [_dag(f"dag_{i:03d}") for i in range(120)]
    rows = _sync(dlt_mod, tmp_path, FakeAirflow(dags=dags, tasks={}, assets=[]))
    assert len(rows) == 120


def _query(url):
    return dict(parse_qsl(urlparse(url).query))
