"""Unit tests for the dbt Cloud dlt connector.

Layers, all runnable in CI without a live dbt Cloud credential:

* DB-free tests for factory validation, host normalization, pagination,
  retry/error handling, job resolution, the incremental run-scan and
  full-pass reconciliation state machine (including empty-sweep guards and
  mid-run-failure safety), and definition/run-outcome rendering.
* A generic document DataItem smoke test (``source="dbt_cloud"``) that
  routes rows through normal cognify.
* dlt-pipeline tests (a hand-rolled fake dbt Cloud session, temp sqlite
  destination) covering the merge/forget-on-delete acceptance criteria:
  initial sync stages the expected rows, tombstoned rows are physically
  removed on resync while unchanged rows are kept, and a mid-run API
  exception propagates and fails the run.

No HTTP-mocking library is used anywhere (matching the rest of this repo's
connectors) — ``FakeDbtCloudSession`` is a plain object mimicking the
``requests.Session`` surface this connector actually calls (``.get(url,
params=..., stream=...)`` returning an object with ``.status_code`` /
``.json()`` / ``.iter_content()``).
"""

import json
import re
from types import SimpleNamespace
from urllib.parse import urlsplit
from uuid import NAMESPACE_OID, uuid5

import pytest
from cognee.tasks.ingestion.resolve_dlt_sources import _build_document_data_item

from cognee_community_connector_dbt_cloud.dbt_cloud import (
    _MAX_PAGINATION_PAGES,
    DBT_CLOUD_SOURCE_NAME,
    _build_name_index,
    _build_tests_index,
    _config_hash,
    _definition_doc_id,
    _fetch_run_results,
    _fetch_run_steps,
    _get_artifact,
    _get_json,
    _is_docs_generate_step,
    _iter_rows,
    _manifest_to_rows,
    _node_to_row,
    _normalize_host,
    _paginate_offset,
    _reconcile_vanished_jobs_and_envs,
    _render_lineage,
    _resolve_account_id,
    _resolve_jobs,
    _run_doc_id,
    _run_to_row,
    _run_tombstone,
    _scan_job_runs,
    _sync_environment_definitions,
    _sync_job_run_outcomes,
    _validate_ids,
    dbt_cloud_source,
)

ACCOUNT_ID = 1


# ---------------------------------------------------------------------------
# Fixtures / fakes
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _clear_dbt_cloud_env(monkeypatch):
    for var in ("DBT_CLOUD_ACCOUNT_ID", "DBT_CLOUD_API_TOKEN", "DBT_CLOUD_HOST"):
        monkeypatch.delenv(var, raising=False)


@pytest.fixture(autouse=True)
def _no_real_sleep(monkeypatch):
    target = "cognee_community_connector_dbt_cloud.dbt_cloud._sleep"
    monkeypatch.setattr(target, lambda seconds: None)


def _job(
    job_id,
    *,
    environment_id=10,
    project_id=100,
    name="Job",
    job_type="scheduled",
    is_system=False,
    state=1,
):
    return {
        "id": job_id,
        "name": name,
        "environment_id": environment_id,
        "project_id": project_id,
        "job_type": job_type,
        "is_system": is_system,
        "state": state,
    }


def _run(
    run_id,
    *,
    job_definition_id,
    environment_id=10,
    project_id=100,
    status=10,
    finished_at="2024-01-01T10:00:00+00:00",
    git_branch="main",
    git_sha="abc123",
    status_message=None,
    run_state=1,  # 1=active, 2=deleted -- the fake's stand-in for the runs-list
    # `state` query filter; unrelated to RunResponse.status (queued/success/etc).
):
    return {
        "id": run_id,
        "job_definition_id": job_definition_id,
        "environment_id": environment_id,
        "project_id": project_id,
        "status": status,
        "finished_at": finished_at,
        "git_branch": git_branch,
        "git_sha": git_sha,
        "status_message": status_message,
        "_fake_run_state": run_state,
    }


def _manifest(
    *,
    project_name="jaffle_shop",
    nodes=None,
    sources=None,
    exposures=None,
    metrics=None,
    parent_map=None,
    child_map=None,
):
    return {
        "metadata": {
            "dbt_schema_version": "https://schemas.getdbt.com/dbt/manifest/v12.json",
            "project_name": project_name,
        },
        "nodes": nodes or {},
        "sources": sources or {},
        "exposures": exposures or {},
        "metrics": metrics or {},
        "parent_map": parent_map or {},
        "child_map": child_map or {},
    }


def _model_node(
    name,
    *,
    package_name="jaffle_shop",
    description="",
    materialized="view",
    database="db",
    schema="main",
    columns=None,
    tags=None,
    meta=None,
):
    return {
        "name": name,
        "resource_type": "model",
        "package_name": package_name,
        "description": description,
        "config": {"materialized": materialized},
        "database": database,
        "schema": schema,
        "alias": name,
        "original_file_path": f"models/{name}.sql",
        "tags": tags or [],
        "meta": meta or {},
        "columns": columns or {},
        "raw_code": f"select * from {name}_raw",
    }


def _test_node(name, *, attached_node, column_name=None, test_name="not_null", kwargs=None):
    return {
        "name": name,
        "resource_type": "test",
        "attached_node": attached_node,
        "column_name": column_name,
        "test_metadata": {"name": test_name, "kwargs": kwargs or {}},
        "depends_on": {"nodes": [attached_node]},
    }


class _FakeResponse:
    def __init__(self, status_code, payload=None, raw_bytes=None):
        self.status_code = status_code
        self._payload = payload
        self._raw_bytes = raw_bytes
        self.headers = {}

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"unexpected status {self.status_code}")

    def iter_content(self, chunk_size=65536):
        data = (
            self._raw_bytes if self._raw_bytes is not None else json.dumps(self._payload).encode()
        )
        for i in range(0, len(data), chunk_size):
            yield data[i : i + chunk_size]


def _envelope(data):
    return {"data": data, "extra": {}, "status": {"is_success": True}}


class FakeDbtCloudSession:
    """Stand-in for a ``requests.Session`` hitting the dbt Cloud Admin API v2.

    Backed by in-memory fixtures keyed by id. Mirrors the real
    ``{data, extra: {pagination: {count, total_count}}, status}`` envelope
    for list/detail endpoints, and serves artifacts as raw JSON bytes (no
    envelope) via streaming ``.iter_content``, matching the real API.

    Runs filtering: ``state=active`` (always sent by the connector) excludes
    runs whose fixture-only ``_fake_run_state`` is 2 ("deleted"), mirroring
    the real runs-list endpoint's ``state`` filter. ``sort_runs=False``
    disables the fake's own ``-finished_at`` sorting, simulating an API that
    doesn't actually honor the requested sort (for the early-stop-disable
    safety test).
    """

    PAGE_SIZE = 2

    def __init__(
        self,
        jobs=None,
        runs=None,
        run_steps=None,
        manifests=None,
        catalogs=None,
        run_results=None,
        sort_runs=True,
    ):
        self.jobs = jobs or {}
        self.runs = runs or {}
        self.run_steps = run_steps or {}
        self.manifests = manifests or {}
        self.catalogs = catalogs or {}
        self.run_results = run_results or {}
        self.sort_runs = sort_runs
        self.calls = []

    def get(self, url, params=None, stream=False):
        self.calls.append((url, params))
        path = urlsplit(url).path
        query = dict(params or {})

        m = re.fullmatch(r"/api/v2/accounts/\d+/jobs/(\d+)/", path)
        if m:
            job = self.jobs.get(int(m.group(1)))
            if job is None:
                return _FakeResponse(404, {"status": {"is_success": False}})
            return _FakeResponse(200, _envelope(job))

        if re.fullmatch(r"/api/v2/accounts/\d+/jobs/", path):
            items = list(self.jobs.values())
            if "environment_id" in query:
                items = [j for j in items if j.get("environment_id") == query["environment_id"]]
            if "project_id" in query:
                items = [j for j in items if j.get("project_id") == query["project_id"]]
            return self._page(items, query)

        m = re.fullmatch(r"/api/v2/accounts/\d+/runs/(\d+)/", path)
        if m:
            run_id = int(m.group(1))
            run = self.runs.get(run_id)
            if run is None:
                return _FakeResponse(404, {"status": {"is_success": False}})
            data = dict(run)
            if "run_steps" in (query.get("include_related") or ""):
                data["run_steps"] = self.run_steps.get(run_id, [])
            return _FakeResponse(200, _envelope(data))

        if re.fullmatch(r"/api/v2/accounts/\d+/runs/", path):
            items = list(self.runs.values())
            if "job_definition_id" in query:
                items = [
                    r for r in items if r.get("job_definition_id") == query["job_definition_id"]
                ]
            if query.get("state") == "active":
                items = [r for r in items if r.get("_fake_run_state", 1) == 1]
            if query.get("order_by") == "-finished_at" and self.sort_runs:
                items = sorted(items, key=lambda r: r.get("finished_at") or "", reverse=True)
            return self._page(items, query)

        m = re.fullmatch(r"/api/v2/accounts/\d+/runs/(\d+)/artifacts/(.+)", path)
        if m:
            run_id, artifact = int(m.group(1)), m.group(2)
            if artifact == "manifest.json":
                manifest = self.manifests.get(run_id)
                return (
                    _FakeResponse(200, raw_bytes=json.dumps(manifest).encode())
                    if manifest is not None
                    else _FakeResponse(404)
                )
            if artifact == "catalog.json":
                catalog = self.catalogs.get(run_id)
                return (
                    _FakeResponse(200, raw_bytes=json.dumps(catalog).encode())
                    if catalog is not None
                    else _FakeResponse(404)
                )
            if artifact == "run_results.json":
                payload = self.run_results.get((run_id, query.get("step")))
                return (
                    _FakeResponse(200, raw_bytes=json.dumps(payload).encode())
                    if payload is not None
                    else _FakeResponse(404)
                )
            return _FakeResponse(404)

        return _FakeResponse(404, {"status": {"is_success": False}})

    def _page(self, items, query):
        offset = int(query.get("offset", 0))
        chunk = items[offset : offset + self.PAGE_SIZE]
        return _FakeResponse(
            200,
            {
                "data": chunk,
                "extra": {"pagination": {"count": len(chunk), "total_count": len(items)}},
                "status": {"is_success": True},
            },
        )


class _BoomOnPathMatch:
    """Delegates to a real fake session except raises when the URL path
    matches a given regex -- used to inject a failure at a specific point
    in a multi-call sync for mid-run-exception safety tests.
    """

    def __init__(self, delegate, path_pattern):
        self._delegate = delegate
        self._pattern = re.compile(path_pattern)

    def get(self, url, params=None, stream=False):
        if self._pattern.search(urlsplit(url).path):
            raise RuntimeError("network boom")
        return self._delegate.get(url, params=params, stream=stream)


class _ScriptedSession:
    """Returns a scripted sequence of responses to successive GET calls."""

    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = 0

    def get(self, url, params=None, stream=False):
        self.calls += 1
        status, payload = self._responses.pop(0)
        return _FakeResponse(status, payload)


class _BoomSession:
    def get(self, url, params=None, stream=False):
        raise RuntimeError("network boom")


def _config(**overrides):
    from cognee_community_connector_dbt_cloud.dbt_cloud import _Config

    defaults = {
        "account_id": ACCOUNT_ID,
        "base_url": "https://cloud.getdbt.com",
        "project_ids": None,
        "environment_ids": (10,),
        "job_ids": None,
        "resource_types": ("models", "sources", "seeds", "snapshots", "exposures", "metrics"),
        "include_packages": False,
        "include_sql": False,
        "include_catalog": True,
        "include_run_outcomes": True,
        "include_ci_jobs": False,
        "max_runs_per_job": 50,
        "max_artifact_mb": 200,
        "full_sync_every": 10,
    }
    defaults.update(overrides)
    return _Config(**defaults)


def _orchestration_session(manifest_nodes=None):
    """A minimal, fully-wired fixture: one job/env/run with a manifest and
    an empty run_results.json -- the baseline for _iter_rows tests, which
    callers mutate (add runs, change manifests) between sync calls.
    """
    manifest = _manifest(
        nodes=manifest_nodes or {"model.jaffle_shop.orders": _model_node("orders")}
    )
    return FakeDbtCloudSession(
        jobs={1: _job(1, environment_id=10)},
        runs={1: _run(1, job_definition_id=1, finished_at="2024-01-01T00:00:00+00:00")},
        manifests={1: manifest},
        run_steps={1: [{"index": 1, "name": "dbt build"}]},
        run_results={(1, 1): {"results": []}},
    )


# ---------------------------------------------------------------------------
# 1. Factory validation
# ---------------------------------------------------------------------------


def test_account_id_required():
    with pytest.raises(ValueError, match="account_id"):
        dbt_cloud_source(environment_ids=[1], api_token="tok", session=FakeDbtCloudSession())


def test_account_id_from_env(monkeypatch):
    monkeypatch.setenv("DBT_CLOUD_ACCOUNT_ID", "42")
    assert _resolve_account_id(None) == 42


def test_account_id_must_be_positive_int():
    with pytest.raises(ValueError, match="positive integer"):
        _resolve_account_id("not-a-number")
    with pytest.raises(ValueError, match="positive integer"):
        _resolve_account_id(-1)


def test_api_token_required_when_no_session():
    with pytest.raises(ValueError, match="requires an API token"):
        dbt_cloud_source(account_id=1, environment_ids=[1])


def test_api_token_from_env(monkeypatch):
    monkeypatch.setenv("DBT_CLOUD_API_TOKEN", "secret-token")
    source = dbt_cloud_source(account_id=1, environment_ids=[1], session=FakeDbtCloudSession())
    assert source is not None


def test_token_never_appears_in_error_message():
    secret = "super-secret-dbt-token"
    with pytest.raises(ValueError) as exc_info:
        dbt_cloud_source(account_id=1, environment_ids=[1], api_token=None)
    assert secret not in str(exc_info.value)


def test_host_normalization_variants():
    assert _normalize_host("cloud.getdbt.com") == "https://cloud.getdbt.com"
    assert _normalize_host("https://cloud.getdbt.com") == "https://cloud.getdbt.com"
    assert _normalize_host("https://cloud.getdbt.com/") == "https://cloud.getdbt.com"
    assert _normalize_host("abc123.us1.dbt.com/") == "https://abc123.us1.dbt.com"
    assert _normalize_host(None) == "https://cloud.getdbt.com"


def test_host_rejects_http():
    with pytest.raises(ValueError, match="https"):
        _normalize_host("http://cloud.getdbt.com")


def test_host_rejects_path_query_or_credentials():
    for bad in ("cloud.getdbt.com/api/v2", "cloud.getdbt.com?x=1", "user:pass@cloud.getdbt.com"):
        with pytest.raises(ValueError, match="bare hostname"):
            _normalize_host(bad)


def test_host_from_env(monkeypatch):
    monkeypatch.setenv("DBT_CLOUD_HOST", "abc123.us1.dbt.com")
    source = dbt_cloud_source(
        account_id=1, environment_ids=[1], api_token="tok", session=FakeDbtCloudSession()
    )
    assert source is not None


def test_resource_types_must_not_be_empty():
    with pytest.raises(ValueError, match="must not be empty"):
        dbt_cloud_source(
            account_id=1,
            environment_ids=[1],
            api_token="tok",
            resource_types=(),
            session=FakeDbtCloudSession(),
        )


def test_invalid_resource_type_raises():
    with pytest.raises(ValueError, match="Invalid resource_types"):
        dbt_cloud_source(
            account_id=1,
            environment_ids=[1],
            api_token="tok",
            resource_types=("models", "bogus"),
            session=FakeDbtCloudSession(),
        )


def test_selection_required():
    with pytest.raises(ValueError, match="at least one of"):
        dbt_cloud_source(account_id=1, api_token="tok", session=FakeDbtCloudSession())


def test_validate_ids_rejects_non_positive_or_non_int():
    with pytest.raises(ValueError, match="positive integers"):
        _validate_ids("job_ids", [1, -2])
    with pytest.raises(ValueError, match="positive integers"):
        _validate_ids("job_ids", [1, "2"])
    with pytest.raises(ValueError, match="positive integers"):
        _validate_ids("job_ids", [])
    assert _validate_ids("job_ids", None) is None
    assert _validate_ids("job_ids", [1, 2]) == (1, 2)


def test_max_runs_per_job_and_max_artifact_mb_validated():
    with pytest.raises(ValueError, match="max_runs_per_job"):
        dbt_cloud_source(
            account_id=1,
            environment_ids=[1],
            api_token="tok",
            max_runs_per_job=0,
            session=FakeDbtCloudSession(),
        )
    with pytest.raises(ValueError, match="max_artifact_mb"):
        dbt_cloud_source(
            account_id=1,
            environment_ids=[1],
            api_token="tok",
            max_artifact_mb=0,
            session=FakeDbtCloudSession(),
        )


def test_full_sync_every_validation():
    with pytest.raises(ValueError, match="full_sync_every"):
        dbt_cloud_source(
            account_id=1,
            environment_ids=[1],
            api_token="tok",
            full_sync_every=0,
            session=FakeDbtCloudSession(),
        )


# ---------------------------------------------------------------------------
# 2. _get_json
# ---------------------------------------------------------------------------


def test_get_json_retries_5xx_then_succeeds():
    session = _ScriptedSession([(503, None), (502, None), (200, _envelope({"ok": True}))])
    assert _get_json(session, "https://x/y")["data"] == {"ok": True}


def test_get_json_429_waits_cooldown_and_retries_twice_max(monkeypatch):
    calls = []
    monkeypatch.setattr(
        "cognee_community_connector_dbt_cloud.dbt_cloud._sleep", lambda s: calls.append(s)
    )
    session = _ScriptedSession([(429, None), (429, None), (200, _envelope({"ok": True}))])
    assert _get_json(session, "https://x/y")["data"] == {"ok": True}
    assert calls == [300, 300]


def test_get_json_429_exhausted_raises():
    session = _ScriptedSession([(429, None), (429, None), (429, None)])
    with pytest.raises(RuntimeError, match="rate limit"):
        _get_json(session, "https://x/y")


def test_get_json_401_message():
    session = _ScriptedSession([(401, None)])
    with pytest.raises(RuntimeError, match="authentication failed"):
        _get_json(session, "https://x/y")


def test_get_json_403_names_permission():
    session = _ScriptedSession([(403, None)])
    with pytest.raises(RuntimeError, match="Read-Only"):
        _get_json(session, "https://x/y")


def test_get_json_404_names_path():
    url = "https://x/accounts/1/jobs/999/"
    session = _ScriptedSession([(404, None)])
    with pytest.raises(RuntimeError, match=re.escape(url)):
        _get_json(session, url)


def test_get_json_connection_error_hints_host():
    session = _BoomSession()
    with pytest.raises(RuntimeError, match="host"):
        _get_json(session, "https://x/y")


def test_get_json_is_success_false_raises_user_message():
    session = _ScriptedSession(
        [(200, {"data": None, "status": {"is_success": False, "user_message": "nope"}})]
    )
    with pytest.raises(RuntimeError, match="nope"):
        _get_json(session, "https://x/y")


# ---------------------------------------------------------------------------
# 3. _paginate_offset
# ---------------------------------------------------------------------------


def test_paginate_offset_multi_page_uses_total_count():
    session = FakeDbtCloudSession(jobs={i: _job(i) for i in range(1, 6)})
    items = list(_paginate_offset(session, "https://cloud.getdbt.com/api/v2/accounts/1/jobs/", {}))
    assert sorted(item["id"] for item in items) == [1, 2, 3, 4, 5]


def test_paginate_offset_stops_on_empty_page():
    session = FakeDbtCloudSession(jobs={})
    items = list(_paginate_offset(session, "https://cloud.getdbt.com/api/v2/accounts/1/jobs/", {}))
    assert items == []


class _RawOffsetSession:
    """A session that slices an in-memory list by the request's own
    ``offset``/``limit`` params, returning a given ``extra`` block verbatim
    (or omitting it) -- used to exercise _paginate_offset's fallback
    behavior when ``total_count`` is missing or malformed.
    """

    def __init__(self, items, extra_fn=lambda chunk: {}):
        self._items = items
        self._extra_fn = extra_fn
        self.calls = 0

    def get(self, url, params=None, stream=False):
        self.calls += 1
        query = params or {}
        offset = int(query.get("offset", 0))
        limit = int(query.get("limit", 100))
        chunk = self._items[offset : offset + limit]
        payload = {"data": chunk, "status": {"is_success": True}}
        extra = self._extra_fn(chunk)
        if extra is not None:
            payload["extra"] = extra
        return _FakeResponse(200, payload)


def test_paginate_offset_without_pagination_metadata_stops_on_short_page():
    # A single page shorter than the requested limit is itself the "short
    # page" signal -- no `extra` key at all is the most degenerate case.
    items = [{"id": i} for i in range(5)]
    session = _RawOffsetSession(items, extra_fn=lambda chunk: None)
    result = list(_paginate_offset(session, "https://cloud.getdbt.com/api/v2/x/", {}))
    assert [item["id"] for item in result] == list(range(5))
    assert session.calls == 1


def test_paginate_offset_without_pagination_metadata_pages_until_short_page():
    items = [{"id": i} for i in range(250)]
    session = _RawOffsetSession(items, extra_fn=lambda chunk: None)
    result = list(_paginate_offset(session, "https://cloud.getdbt.com/api/v2/x/", {}))
    assert len(result) == 250
    assert session.calls == 3  # 100 + 100 + 50 (short page stops it)


def test_paginate_offset_non_int_total_count_falls_back_to_short_page():
    items = [{"id": i} for i in range(5)]
    session = _RawOffsetSession(
        items, extra_fn=lambda chunk: {"pagination": {"total_count": "not-a-number"}}
    )
    result = list(_paginate_offset(session, "https://cloud.getdbt.com/api/v2/x/", {}))
    assert len(result) == 5
    assert session.calls == 1  # never trusted total_count alone -> short page stopped it


def test_paginate_offset_page_cap_trips_on_runaway_listing():
    # A page that is always exactly `limit` items long, with no usable
    # `total_count`, never naturally terminates -- the hard page cap must.
    session = _RawOffsetSession(
        [{"id": i} for i in range(_MAX_PAGINATION_PAGES * 100 + 1000)], extra_fn=lambda chunk: None
    )
    url = "https://cloud.getdbt.com/api/v2/accounts/1/jobs/"
    with pytest.raises(RuntimeError, match=re.escape(url)):
        list(_paginate_offset(session, url, {}))
    assert session.calls == _MAX_PAGINATION_PAGES


# NOTE: _paginate_offset's "offset did not advance" guard is defensive-only
# and not independently tested: given the function's own arithmetic
# (offset += len(items), only reached after an `if not items: return`
# early-out), a non-empty page can never fail to advance offset by at
# least 1. Kept as cheap insurance against a future refactor, not a tested
# code path.


# ---------------------------------------------------------------------------
# 4. _get_artifact
# ---------------------------------------------------------------------------


def test_get_artifact_size_cap_enforced():
    big_payload = {"nodes": {f"model.x.m{i}" + "a" * 1000: {} for i in range(2000)}}
    session = FakeDbtCloudSession(manifests={1: big_payload})
    with pytest.raises(RuntimeError, match="max_artifact_mb"):
        _get_artifact(
            session,
            "https://cloud.getdbt.com/api/v2/accounts/1/runs/1/artifacts/manifest.json",
            None,
            max_bytes=10,
            optional=False,
        )


def test_get_artifact_optional_404_returns_none():
    session = FakeDbtCloudSession(manifests={})
    result = _get_artifact(
        session,
        "https://cloud.getdbt.com/api/v2/accounts/1/runs/1/artifacts/catalog.json",
        None,
        max_bytes=10_000_000,
        optional=True,
    )
    assert result is None


def test_get_artifact_manifest_404_is_error():
    session = FakeDbtCloudSession(manifests={})
    with pytest.raises(RuntimeError, match="not found"):
        _get_artifact(
            session,
            "https://cloud.getdbt.com/api/v2/accounts/1/runs/1/artifacts/manifest.json",
            None,
            max_bytes=10_000_000,
            optional=False,
        )


# ---------------------------------------------------------------------------
# 5. Job resolution
# ---------------------------------------------------------------------------


def test_resolve_jobs_explicit_ids():
    session = FakeDbtCloudSession(jobs={1: _job(1), 2: _job(2)})
    jobs = _resolve_jobs(session, _config(environment_ids=None, job_ids=(1, 2)))
    assert sorted(j["id"] for j in jobs) == [1, 2]


def test_resolve_jobs_explicit_missing_id_errors():
    session = FakeDbtCloudSession(jobs={1: _job(1)})
    with pytest.raises(RuntimeError, match="not found"):
        _resolve_jobs(session, _config(environment_ids=None, job_ids=(999,)))


def test_resolve_jobs_explicit_inactive_id_errors():
    session = FakeDbtCloudSession(jobs={1: _job(1, state=2)})
    with pytest.raises(RuntimeError, match="deleted or inactive"):
        _resolve_jobs(session, _config(environment_ids=None, job_ids=(1,)))


def test_resolve_jobs_by_environment():
    session = FakeDbtCloudSession(
        jobs={1: _job(1, environment_id=10), 2: _job(2, environment_id=20)}
    )
    jobs = _resolve_jobs(session, _config(environment_ids=(10,)))
    assert [j["id"] for j in jobs] == [1]


def test_resolve_jobs_tolerates_job_listing_with_no_pagination_metadata():
    # _list_jobs_by_scope delegates straight to _paginate_offset with no
    # pagination handling of its own -- confirms the short-page fallback
    # (added for when `extra.pagination.total_count` is absent/malformed)
    # actually terminates a real job-resolution call, not just the
    # standalone _paginate_offset unit tests above.
    jobs_by_id = {1: _job(1, environment_id=10), 2: _job(2, environment_id=10)}

    class _NoMetadataJobSession:
        def get(self, url, params=None, stream=False):
            offset = int((params or {}).get("offset", 0))
            limit = int((params or {}).get("limit", 100))
            chunk = list(jobs_by_id.values())[offset : offset + limit]
            return _FakeResponse(200, {"data": chunk, "status": {"is_success": True}})

    jobs = _resolve_jobs(_NoMetadataJobSession(), _config(environment_ids=(10,)))
    assert sorted(j["id"] for j in jobs) == [1, 2]


def test_resolve_jobs_by_environment_filtered_by_project_locally():
    session = FakeDbtCloudSession(
        jobs={
            1: _job(1, environment_id=10, project_id=100),
            2: _job(2, environment_id=10, project_id=200),
        }
    )
    jobs = _resolve_jobs(session, _config(environment_ids=(10,), project_ids=(100,)))
    assert [j["id"] for j in jobs] == [1]


def test_resolve_jobs_by_project():
    session = FakeDbtCloudSession(jobs={1: _job(1, project_id=100), 2: _job(2, project_id=200)})
    jobs = _resolve_jobs(session, _config(environment_ids=None, project_ids=(100,)))
    assert [j["id"] for j in jobs] == [1]


def test_resolve_jobs_dedupes():
    session = FakeDbtCloudSession(jobs={1: _job(1, environment_id=10, project_id=100)})
    jobs = _resolve_jobs(session, _config(environment_ids=(10,), project_ids=(100,)))
    assert [j["id"] for j in jobs] == [1]


def test_resolve_jobs_excludes_ci_and_merge_by_default():
    session = FakeDbtCloudSession(
        jobs={
            1: _job(1, job_type="scheduled"),
            2: _job(2, job_type="ci"),
            3: _job(3, job_type="merge"),
            4: _job(4, job_type="other"),
        }
    )
    jobs = _resolve_jobs(session, _config())
    assert sorted(j["id"] for j in jobs) == [1, 4]


def test_resolve_jobs_includes_ci_when_opted_in():
    session = FakeDbtCloudSession(jobs={1: _job(1, job_type="ci")})
    jobs = _resolve_jobs(session, _config(include_ci_jobs=True))
    assert [j["id"] for j in jobs] == [1]


def test_resolve_jobs_excludes_system_jobs():
    session = FakeDbtCloudSession(jobs={1: _job(1, is_system=True), 2: _job(2)})
    jobs = _resolve_jobs(session, _config())
    assert [j["id"] for j in jobs] == [2]


def test_resolve_jobs_explicit_ci_job_dropped_with_warning(caplog):
    session = FakeDbtCloudSession(jobs={1: _job(1, job_type="ci")})
    jobs = _resolve_jobs(session, _config(environment_ids=None, job_ids=(1,)))
    assert jobs == []
    assert any(
        "dropping it" in r.message and "ci" in r.message and "include_ci_jobs" in r.message
        for r in caplog.records
    )


def test_resolve_jobs_explicit_system_job_dropped_with_warning(caplog):
    session = FakeDbtCloudSession(jobs={1: _job(1, is_system=True)})
    jobs = _resolve_jobs(session, _config(environment_ids=None, job_ids=(1,)))
    assert jobs == []
    assert any("system job" in r.message for r in caplog.records)


# ---------------------------------------------------------------------------
# 6. Run scanning (_scan_job_runs)
# ---------------------------------------------------------------------------


def test_scan_job_runs_full_pass_finds_latest_success_and_window():
    session = FakeDbtCloudSession(
        runs={
            1: _run(1, job_definition_id=1, status=10, finished_at="2024-01-03T00:00:00+00:00"),
            2: _run(2, job_definition_id=1, status=20, finished_at="2024-01-02T00:00:00+00:00"),
            3: _run(3, job_definition_id=1, status=10, finished_at="2024-01-01T00:00:00+00:00"),
        }
    )
    finished_runs, latest_success = _scan_job_runs(session, _config(), _job(1), cursor=None)
    assert [r["id"] for r in finished_runs] == [1, 2, 3]
    assert latest_success["id"] == 1


def test_scan_job_runs_tolerates_run_listing_with_no_pagination_metadata():
    # _scan_job_runs also delegates straight to _paginate_offset -- confirms
    # the same short-page fallback terminates a real run-scan call.
    runs_by_id = {
        1: _run(1, job_definition_id=1, status=10, finished_at="2024-01-02T00:00:00+00:00"),
        2: _run(2, job_definition_id=1, status=10, finished_at="2024-01-01T00:00:00+00:00"),
    }

    class _NoMetadataRunSession:
        def get(self, url, params=None, stream=False):
            offset = int((params or {}).get("offset", 0))
            limit = int((params or {}).get("limit", 100))
            chunk = sorted(runs_by_id.values(), key=lambda r: r["finished_at"], reverse=True)[
                offset : offset + limit
            ]
            return _FakeResponse(200, {"data": chunk, "status": {"is_success": True}})

    finished_runs, latest_success = _scan_job_runs(
        _NoMetadataRunSession(), _config(), _job(1), cursor=None
    )
    assert [r["id"] for r in finished_runs] == [1, 2]
    assert latest_success["id"] == 1


def test_scan_job_runs_full_pass_searches_past_window_for_success():
    session = FakeDbtCloudSession(
        runs={
            1: _run(1, job_definition_id=1, status=20, finished_at="2024-01-02T00:00:00+00:00"),
            2: _run(2, job_definition_id=1, status=10, finished_at="2024-01-01T00:00:00+00:00"),
        }
    )
    finished_runs, latest_success = _scan_job_runs(
        session, _config(max_runs_per_job=1), _job(1), cursor=None
    )
    assert len(finished_runs) == 1  # outcomes window capped at 1
    assert latest_success["id"] == 2  # but the search kept going to find a success


def test_scan_job_runs_no_success_found_returns_none_without_raising():
    session = FakeDbtCloudSession(
        runs={1: _run(1, job_definition_id=1, status=20, finished_at="2024-01-01T00:00:00+00:00")}
    )
    finished_runs, latest_success = _scan_job_runs(session, _config(), _job(1), cursor=None)
    assert latest_success is None
    assert len(finished_runs) == 1


def test_scan_job_runs_excludes_deleted_runs_via_state_active():
    session = FakeDbtCloudSession(
        runs={
            1: _run(1, job_definition_id=1, status=10, finished_at="2024-01-02T00:00:00+00:00"),
            2: _run(
                2,
                job_definition_id=1,
                status=10,
                finished_at="2024-01-01T00:00:00+00:00",
                run_state=2,
            ),
        }
    )
    finished_runs, _ = _scan_job_runs(session, _config(), _job(1), cursor=None)
    assert [r["id"] for r in finished_runs] == [1]
    first_call_params = session.calls[0][1]
    assert first_call_params.get("state") == "active"


def test_scan_job_runs_ignores_unfinished_runs():
    session = FakeDbtCloudSession(
        runs={
            1: _run(1, job_definition_id=1, status=3, finished_at=None),
            2: _run(2, job_definition_id=1, status=10),
        }
    )
    finished_runs, _latest_success = _scan_job_runs(session, _config(), _job(1), cursor=None)
    assert [r["id"] for r in finished_runs] == [2]


def test_scan_job_runs_incremental_stops_at_cursor_with_ties_included():
    session = FakeDbtCloudSession(
        runs={
            1: _run(1, job_definition_id=1, finished_at="2024-01-03T00:00:00+00:00"),
            2: _run(2, job_definition_id=1, finished_at="2024-01-02T00:00:00+00:00"),  # tie
            3: _run(3, job_definition_id=1, finished_at="2024-01-01T00:00:00+00:00"),
        }
    )
    finished_runs, _ = _scan_job_runs(
        session, _config(), _job(1), cursor="2024-01-02T00:00:00+00:00"
    )
    assert sorted(r["id"] for r in finished_runs) == [1, 2]


def test_scan_job_runs_disables_early_stop_on_sort_violation(caplog):
    session = FakeDbtCloudSession(
        runs={
            1: _run(1, job_definition_id=1, finished_at="2024-01-01T00:00:00+00:00"),  # old, first
            2: _run(2, job_definition_id=1, finished_at="2024-01-05T00:00:00+00:00"),  # violation
            3: _run(3, job_definition_id=1, finished_at="2024-01-03T00:00:00+00:00"),
        },
        sort_runs=False,
    )
    finished_runs, _ = _scan_job_runs(
        session, _config(), _job(1), cursor="2024-01-02T00:00:00+00:00"
    )
    assert sorted(r["id"] for r in finished_runs) == [2, 3]
    assert any("not sorted" in r.message for r in caplog.records)


def test_scan_job_runs_mixed_utc_offsets_and_microseconds_compare_correctly():
    session = FakeDbtCloudSession(
        runs={
            1: _run(1, job_definition_id=1, finished_at="2024-01-01T09:00:00-01:00"),  # ==10:00 UTC
            2: _run(2, job_definition_id=1, finished_at="2024-01-01T10:00:00.500000+00:00"),
        }
    )
    finished_runs, _ = _scan_job_runs(
        session, _config(), _job(1), cursor="2024-01-01T10:00:00+00:00"
    )
    assert sorted(r["id"] for r in finished_runs) == [1, 2]  # tie and later-with-microseconds


# ---------------------------------------------------------------------------
# _config_hash
# ---------------------------------------------------------------------------


def test_config_hash_stable_for_same_config():
    assert _config_hash(_config()) == _config_hash(_config())


def test_config_hash_changes_with_resource_types():
    assert _config_hash(_config()) != _config_hash(_config(resource_types=("models",)))


def test_config_hash_changes_with_include_sql():
    assert _config_hash(_config()) != _config_hash(_config(include_sql=True))


def test_config_hash_excludes_host_and_full_sync_every_and_max_artifact_mb():
    assert _config_hash(_config(base_url="https://other.dbt.com")) == _config_hash(_config())
    assert _config_hash(_config(full_sync_every=1)) == _config_hash(_config())
    assert _config_hash(_config(max_artifact_mb=1)) == _config_hash(_config())


# ---------------------------------------------------------------------------
# Environment definition sync (_sync_environment_definitions)
# ---------------------------------------------------------------------------


def test_sync_environment_definitions_full_pass_fetches_and_renders():
    manifest = _manifest(nodes={"model.jaffle_shop.orders": _model_node("orders")})
    session = FakeDbtCloudSession(manifests={1: manifest})
    run = _run(1, job_definition_id=1, finished_at="2024-01-01T00:00:00+00:00")
    rows, new_state = _sync_environment_definitions(session, _config(), 10, [run], {}, is_full=True)
    assert len(rows) == 1
    assert new_state["manifest_run_id"] == 1
    assert new_state["node_ids"] == [_definition_doc_id(ACCOUNT_ID, 10, "model.jaffle_shop.orders")]


def test_sync_environment_definitions_incremental_skips_when_run_unchanged():
    session = FakeDbtCloudSession()
    run = _run(1, job_definition_id=1, finished_at="2024-01-01T00:00:00+00:00")
    prior = {"manifest_run_id": 1, "node_ids": ["x"]}
    rows, new_state = _sync_environment_definitions(
        session, _config(), 10, [run], prior, is_full=False
    )
    assert rows == []
    assert new_state == prior
    assert session.calls == []


def test_sync_environment_definitions_full_pass_always_refetches_even_if_run_unchanged():
    manifest = _manifest(nodes={"model.jaffle_shop.orders": _model_node("orders")})
    session = FakeDbtCloudSession(manifests={1: manifest})
    run = _run(1, job_definition_id=1, finished_at="2024-01-01T00:00:00+00:00")
    prior = {
        "manifest_run_id": 1,
        "node_ids": [_definition_doc_id(ACCOUNT_ID, 10, "model.jaffle_shop.orders")],
    }
    rows, _new_state = _sync_environment_definitions(
        session, _config(), 10, [run], prior, is_full=True
    )
    assert session.calls  # re-fetched despite unchanged run id -- picks up config changes
    assert len(rows) == 1


def test_sync_environment_definitions_tombstones_removed_nodes():
    manifest = _manifest(nodes={"model.jaffle_shop.orders": _model_node("orders")})
    session = FakeDbtCloudSession(manifests={1: manifest})
    run = _run(1, job_definition_id=1, finished_at="2024-01-02T00:00:00+00:00")
    prior = {
        "manifest_run_id": 99,
        "node_ids": [
            _definition_doc_id(ACCOUNT_ID, 10, "model.jaffle_shop.orders"),
            _definition_doc_id(ACCOUNT_ID, 10, "model.jaffle_shop.customers"),
        ],
    }
    rows, new_state = _sync_environment_definitions(
        session, _config(), 10, [run], prior, is_full=True
    )
    tombstoned = [r["id"] for r in rows if r["_deleted"]]
    assert tombstoned == [_definition_doc_id(ACCOUNT_ID, 10, "model.jaffle_shop.customers")]
    assert "model.jaffle_shop.customers" not in str(new_state["node_ids"])


def test_sync_environment_definitions_no_success_candidate_keeps_prior():
    prior = {"manifest_run_id": 5, "node_ids": ["x"]}
    rows, new_state = _sync_environment_definitions(
        FakeDbtCloudSession(), _config(), 10, [None], prior, is_full=False
    )
    assert rows == []
    assert new_state == prior


def test_sync_environment_definitions_empty_manifest_guard():
    session = FakeDbtCloudSession(manifests={1: _manifest(nodes={})})
    run = _run(1, job_definition_id=1, finished_at="2024-01-02T00:00:00+00:00")
    prior = {"manifest_run_id": 99, "node_ids": ["dbt-cloud:1:10:model.x.orders"]}
    rows, new_state = _sync_environment_definitions(
        session, _config(), 10, [run], prior, is_full=True
    )
    assert rows == []
    assert new_state == prior


# ---------------------------------------------------------------------------
# 7. Definition rendering
# ---------------------------------------------------------------------------


def test_manifest_to_rows_filters_by_resource_type():
    manifest = _manifest(nodes={"model.jaffle_shop.orders": _model_node("orders")})
    rows = list(_manifest_to_rows(_config(resource_types=("sources",)), 10, manifest, None))
    assert rows == []
    rows = list(_manifest_to_rows(_config(resource_types=("models",)), 10, manifest, None))
    assert len(rows) == 1


def test_manifest_to_rows_excludes_packages_by_default():
    manifest = _manifest(
        nodes={
            "model.jaffle_shop.orders": _model_node("orders", package_name="jaffle_shop"),
            "model.dbt_utils.helper": _model_node("helper", package_name="dbt_utils"),
        }
    )
    rows = list(_manifest_to_rows(_config(), 10, manifest, None))
    assert len(rows) == 1
    assert "orders" in rows[0]["title"]


def test_manifest_to_rows_includes_packages_when_opted_in():
    manifest = _manifest(
        nodes={
            "model.jaffle_shop.orders": _model_node("orders", package_name="jaffle_shop"),
            "model.dbt_utils.helper": _model_node("helper", package_name="dbt_utils"),
        }
    )
    rows = list(_manifest_to_rows(_config(include_packages=True), 10, manifest, None))
    assert len(rows) == 2


def test_manifest_to_rows_missing_project_name_includes_everything_with_warning(caplog):
    manifest = _manifest(
        project_name=None, nodes={"model.x.orders": _model_node("orders", package_name="x")}
    )
    rows = list(_manifest_to_rows(_config(), 10, manifest, None))
    assert len(rows) == 1
    assert any("no project_name" in r.message for r in caplog.records)


def test_node_to_row_attaches_tests_and_lineage():
    manifest = _manifest(
        nodes={
            "model.jaffle_shop.customers": _model_node("customers"),
            "model.jaffle_shop.orders": _model_node("orders"),
            "test.jaffle_shop.not_null_orders_id": _test_node(
                "not_null_orders_id", attached_node="model.jaffle_shop.orders", column_name="id"
            ),
        },
        parent_map={"model.jaffle_shop.orders": ["model.jaffle_shop.customers"]},
        child_map={"model.jaffle_shop.customers": ["model.jaffle_shop.orders"]},
    )
    rows = {
        r["id"]: r
        for r in _manifest_to_rows(_config(resource_types=("models",)), 10, manifest, None)
    }
    orders_row = rows[_definition_doc_id(ACCOUNT_ID, 10, "model.jaffle_shop.orders")]
    # The rendered test summary uses the generic test's *macro* name
    # (test_metadata.name, e.g. "not_null"), not the test node's own
    # per-column unique name ("not_null_orders_id") -- confirmed against a
    # real dbt-core manifest.json during this connector's research.
    assert "not_null on column id" in orders_row["content"]
    assert "Upstream: customers (model.jaffle_shop.customers)" in orders_row["content"]

    customers_row = rows[_definition_doc_id(ACCOUNT_ID, 10, "model.jaffle_shop.customers")]
    assert "Downstream: orders (model.jaffle_shop.orders)" in customers_row["content"]


def test_lineage_cap_at_100():
    ids = [f"model.x.m{i}" for i in range(150)]
    names = {i: i.split(".")[-1] for i in ids}
    text = _render_lineage("Upstream", ids, names)
    assert "(+50 more)" in text


def test_include_sql_toggle():
    manifest = _manifest(nodes={"model.jaffle_shop.orders": _model_node("orders")})
    rows_off = list(
        _manifest_to_rows(
            _config(include_sql=False, resource_types=("models",)), 10, manifest, None
        )
    )
    rows_on = list(
        _manifest_to_rows(_config(include_sql=True, resource_types=("models",)), 10, manifest, None)
    )
    assert "Source SQL" not in rows_off[0]["content"]
    assert "Source SQL" in rows_on[0]["content"]


def test_catalog_column_types_joined_when_available():
    manifest = _manifest(
        nodes={
            "model.jaffle_shop.orders": _model_node(
                "orders", columns={"id": {"description": "the id"}}
            )
        }
    )
    catalog = {"nodes": {"model.jaffle_shop.orders": {"columns": {"id": {"type": "INTEGER"}}}}}
    rows = list(_manifest_to_rows(_config(resource_types=("models",)), 10, manifest, catalog))
    assert "id (INTEGER): the id" in rows[0]["content"]


def test_catalog_missing_is_tolerated():
    manifest = _manifest(
        nodes={
            "model.jaffle_shop.orders": _model_node("orders", columns={"id": {"description": "x"}})
        }
    )
    rows = list(_manifest_to_rows(_config(resource_types=("models",)), 10, manifest, None))
    assert "id: x" in rows[0]["content"]


def test_node_to_row_defensive_against_missing_keys():
    row = _node_to_row(_config(), 10, "model.x.bare", {}, "model", {}, {}, {}, {}, {})
    assert row["id"] == _definition_doc_id(ACCOUNT_ID, 10, "model.x.bare")
    assert row["content"]
    assert row["_deleted"] is False


def test_rendering_is_deterministic():
    manifest = _manifest(nodes={"model.jaffle_shop.orders": _model_node("orders", description="d")})
    rows1 = list(_manifest_to_rows(_config(resource_types=("models",)), 10, manifest, None))
    rows2 = list(_manifest_to_rows(_config(resource_types=("models",)), 10, manifest, None))
    assert rows1 == rows2


def test_build_name_index_excludes_tests_and_macros():
    manifest = _manifest(
        nodes={
            "model.x.orders": _model_node("orders"),
            "test.x.t1": _test_node("t1", attached_node="model.x.orders"),
        }
    )
    names = _build_name_index(manifest)
    assert "model.x.orders" in names
    assert "test.x.t1" not in names


def test_build_tests_index_falls_back_to_single_depends_on_node():
    test = _test_node("unique_orders_id", attached_node=None, test_name="unique")
    test["depends_on"] = {"nodes": ["model.x.orders"]}
    manifest = _manifest(nodes={"test.x.unique_orders_id": test})
    index = _build_tests_index(manifest)
    assert "unique" in index["model.x.orders"][0]


def test_build_tests_index_skips_ambiguous_multi_dependency_test():
    test = _test_node("relationships", attached_node=None)
    test["depends_on"] = {"nodes": ["model.x.orders", "model.x.customers"]}
    manifest = _manifest(nodes={"test.x.relationships": test})
    index = _build_tests_index(manifest)
    assert index == {}


def test_build_tests_index_filters_model_kwarg_noise():
    # Verified against a real dbt-core manifest.json during this connector's
    # research: every generic test's test_metadata.kwargs includes a "model"
    # key holding a raw jinja macro-call string -- not useful summary text.
    test = _test_node(
        "unique_orders_id",
        attached_node="model.x.orders",
        test_name="unique",
        kwargs={"column_name": "order_id", "model": "{{ get_where_subquery(ref('orders')) }}"},
    )
    manifest = _manifest(nodes={"test.x.unique_orders_id": test})
    index = _build_tests_index(manifest)
    assert "get_where_subquery" not in index["model.x.orders"][0]


def test_build_tests_index_keeps_meaningful_kwargs():
    test = _test_node(
        "accepted_values_customer_type",
        attached_node="model.x.customers",
        column_name="customer_type",
        test_name="accepted_values",
        kwargs={
            "column_name": "customer_type",
            "values": ["new", "returning"],
            "model": "{{ get_where_subquery(ref('customers')) }}",
        },
    )
    manifest = _manifest(nodes={"test.x.accepted_values_customer_type": test})
    index = _build_tests_index(manifest)
    assert "values=" in index["model.x.customers"][0]


# ---------------------------------------------------------------------------
# 8. Run-outcome sync (_sync_job_run_outcomes)
# ---------------------------------------------------------------------------


def test_sync_job_run_outcomes_new_run_is_rendered():
    run = _run(1, job_definition_id=1, finished_at="2024-01-01T00:00:00+00:00")
    session = FakeDbtCloudSession(
        runs={1: run},
        run_steps={1: [{"index": 1, "name": "dbt build"}]},
        run_results={(1, 1): {"results": []}},
    )
    rows, new_state = _sync_job_run_outcomes(session, _config(), _job(1), [run], {}, is_full=True)
    assert len(rows) == 1
    assert new_state["runs"] == {"1": "2024-01-01T00:00:00+00:00"}
    assert new_state["cursor"] == "2024-01-01T00:00:00+00:00"


def test_sync_job_run_outcomes_already_stored_run_not_refetched():
    session = FakeDbtCloudSession()
    run = _run(1, job_definition_id=1, finished_at="2024-01-01T00:00:00+00:00")
    prior = {"cursor": "2024-01-01T00:00:00+00:00", "runs": {"1": "2024-01-01T00:00:00+00:00"}}
    rows, new_state = _sync_job_run_outcomes(
        session, _config(), _job(1), [run], prior, is_full=False
    )
    assert rows == []  # a finished run's outcome never changes
    assert session.calls == []
    assert new_state["runs"] == {"1": "2024-01-01T00:00:00+00:00"}


def test_sync_job_run_outcomes_window_slide_tombstones_oldest():
    new_run = _run(2, job_definition_id=1, finished_at="2024-01-02T00:00:00+00:00")
    session = FakeDbtCloudSession(
        runs={2: new_run},
        run_steps={2: [{"index": 1, "name": "dbt build"}]},
        run_results={(2, 1): {"results": []}},
    )
    prior = {"cursor": "2024-01-01T00:00:00+00:00", "runs": {"1": "2024-01-01T00:00:00+00:00"}}
    rows, new_state = _sync_job_run_outcomes(
        session, _config(max_runs_per_job=1), _job(1), [new_run], prior, is_full=False
    )
    tombstoned = [r["id"] for r in rows if r["_deleted"]]
    assert tombstoned == [_run_doc_id(ACCOUNT_ID, 1)]
    assert new_state["runs"] == {"2": "2024-01-02T00:00:00+00:00"}


def test_sync_job_run_outcomes_tie_on_cursor_no_duplicate_no_refetch():
    session = FakeDbtCloudSession()
    run = _run(1, job_definition_id=1, finished_at="2024-01-01T00:00:00+00:00")
    prior = {"cursor": "2024-01-01T00:00:00+00:00", "runs": {"1": "2024-01-01T00:00:00+00:00"}}
    rows, new_state = _sync_job_run_outcomes(
        session, _config(), _job(1), [run], prior, is_full=False
    )
    assert rows == []
    assert new_state["runs"] == {"1": "2024-01-01T00:00:00+00:00"}


def test_sync_job_run_outcomes_deleted_run_in_window_is_tombstoned():
    # run 1 gets deleted upstream (the state=active scan no longer returns
    # it); run 2 remains -- a full pass's fresh listing is authoritative.
    session = FakeDbtCloudSession(
        run_steps={2: [{"index": 1, "name": "dbt build"}]}, run_results={(2, 1): {"results": []}}
    )
    prior = {
        "cursor": "2024-01-02T00:00:00+00:00",
        "runs": {"1": "2024-01-01T00:00:00+00:00", "2": "2024-01-02T00:00:00+00:00"},
    }
    remaining_run = _run(2, job_definition_id=1, finished_at="2024-01-02T00:00:00+00:00")
    rows, new_state = _sync_job_run_outcomes(
        session, _config(), _job(1), [remaining_run], prior, is_full=True
    )
    tombstoned = [r["id"] for r in rows if r["_deleted"]]
    assert tombstoned == [_run_doc_id(ACCOUNT_ID, 1)]
    assert new_state["runs"] == {"2": "2024-01-02T00:00:00+00:00"}


def test_sync_job_run_outcomes_empty_sweep_guard_on_full_pass():
    prior = {"cursor": "2024-01-01T00:00:00+00:00", "runs": {"1": "2024-01-01T00:00:00+00:00"}}
    rows, new_state = _sync_job_run_outcomes(
        FakeDbtCloudSession(), _config(), _job(1), [], prior, is_full=True
    )
    assert rows == []
    assert new_state == prior


def test_sync_job_run_outcomes_include_run_outcomes_false_tombstones_all():
    prior = {"cursor": "2024-01-01T00:00:00+00:00", "runs": {"1": "x", "2": "y"}}
    rows, new_state = _sync_job_run_outcomes(
        FakeDbtCloudSession(),
        _config(include_run_outcomes=False),
        _job(1),
        [],
        prior,
        is_full=True,
    )
    tombstoned = sorted(r["id"] for r in rows if r["_deleted"])
    assert tombstoned == sorted([_run_doc_id(ACCOUNT_ID, 1), _run_doc_id(ACCOUNT_ID, 2)])
    assert new_state == {"cursor": None, "runs": {}}


def test_is_docs_generate_step_matches_common_names():
    assert _is_docs_generate_step({"name": "Generate docs on run"})
    assert _is_docs_generate_step({"name": "dbt docs generate"})
    assert not _is_docs_generate_step({"name": "dbt build"})


def test_fetch_run_results_merges_across_steps_and_skips_docs_generate():
    session = FakeDbtCloudSession(
        runs={1: _run(1, job_definition_id=1)},
        run_steps={
            1: [{"index": 1, "name": "dbt build"}, {"index": 2, "name": "dbt docs generate"}]
        },
        run_results={
            (1, 1): {"results": [{"unique_id": "model.x.orders", "status": "pass"}]},
            (1, 2): {"results": [{"unique_id": "model.x.orders", "status": "should-not-be-used"}]},
        },
    )
    steps = _fetch_run_steps(session, _config(), 1)
    results = _fetch_run_results(session, _config(), 1, steps)
    assert len(results) == 1
    assert results[0]["status"] == "pass"


def test_fetch_run_results_tolerates_missing_step_artifact():
    session = FakeDbtCloudSession(
        runs={1: _run(1, job_definition_id=1)},
        run_steps={1: [{"index": 1, "name": "dbt build"}]},
        run_results={},
    )
    steps = _fetch_run_steps(session, _config(), 1)
    results = _fetch_run_results(session, _config(), 1, steps)
    assert results == []


def test_run_to_row_caps_failing_nodes_and_trims_message():
    session = FakeDbtCloudSession(
        runs={1: _run(1, job_definition_id=1)},
        run_steps={1: [{"index": 1, "name": "dbt build"}]},
        run_results={
            (1, 1): {
                "results": [
                    {"unique_id": f"model.x.m{i}", "status": "fail", "message": "x" * 600}
                    for i in range(25)
                ]
            }
        },
    )
    row = _run_to_row(session, _config(), _job(1), _run(1, job_definition_id=1))
    shown = row["content"].count(" [fail]: ")
    assert shown == 20
    assert "(+5 more)" in row["content"]
    assert "x" * 501 not in row["content"]
    assert row["_deleted"] is False


def test_run_doc_id_shape():
    assert _run_doc_id(1, 42) == "dbt-cloud:1:run:42"


def test_run_tombstone_shape():
    assert _run_tombstone(1, 42) == {"id": "dbt-cloud:1:run:42", "_deleted": True}


# ---------------------------------------------------------------------------
# Vanished jobs/environments reconciliation
# ---------------------------------------------------------------------------


def test_reconcile_vanished_jobs_tombstones_runs_and_drops_from_state():
    state = {"jobs": {"1": {"runs": {"5": "x"}}, "2": {"runs": {}}}, "envs": {}}
    rows = _reconcile_vanished_jobs_and_envs(state, _config(), jobs=[_job(2)])
    assert rows == [_run_tombstone(ACCOUNT_ID, 5)]
    assert "1" not in state["jobs"]
    assert "2" in state["jobs"]


def test_reconcile_vanished_envs_tombstones_nodes_and_drops_from_state():
    state = {
        "jobs": {"1": {"runs": {}}},
        "envs": {"10": {"node_ids": ["n1"]}, "20": {"node_ids": ["n2"]}},
    }
    rows = _reconcile_vanished_jobs_and_envs(state, _config(), jobs=[_job(1, environment_id=10)])
    assert rows == [{"id": "n2", "_deleted": True}]
    assert "20" not in state["envs"]
    assert "10" in state["envs"]


def test_reconcile_vanished_jobs_empty_resolved_set_guard():
    state = {"jobs": {"1": {"runs": {"5": "x"}}}, "envs": {}}
    rows = _reconcile_vanished_jobs_and_envs(state, _config(), jobs=[])
    assert rows == []
    assert "1" in state["jobs"]


# ---------------------------------------------------------------------------
# Row shape / document marker / DataItem smoke test
# ---------------------------------------------------------------------------


def test_dbt_cloud_source_declares_document_marker():
    from cognee.tasks.ingestion.dlt_utils import document_source_tag

    source = dbt_cloud_source(
        account_id=1, environment_ids=[10], api_token="tok", session=FakeDbtCloudSession()
    )
    assert DBT_CLOUD_SOURCE_NAME == "dbt_cloud"
    assert document_source_tag(source) == "dbt_cloud"


def test_build_document_data_item_tags_source():
    row = SimpleNamespace(
        row_data={
            "id": "dbt-cloud:1:10:model.jaffle_shop.orders",
            "title": "[model] orders",
            "content": "Resource type: model",
        },
        content_hash="abc123",
    )
    data_id = uuid5(NAMESPACE_OID, "dbt-cloud:1:10:model.jaffle_shop.orders")
    item = _build_document_data_item(row, data_id, "dbt_cloud")
    assert item.external_metadata["source"] == "dbt_cloud"
    assert item.data_id == data_id
    assert item.data.startswith("# [model] orders")


# ---------------------------------------------------------------------------
# _iter_rows orchestration
# ---------------------------------------------------------------------------


def test_first_run_is_a_full_pass_and_populates_state():
    session = _orchestration_session()
    state = {}
    rows = list(_iter_rows(session, _config(), state))
    assert any(
        r["id"] == _definition_doc_id(ACCOUNT_ID, 10, "model.jaffle_shop.orders") for r in rows
    )
    assert any(r["id"] == _run_doc_id(ACCOUNT_ID, 1) for r in rows)
    assert state["runs_since_full"] == 0
    assert state["envs"]["10"]["manifest_run_id"] == 1
    assert state["jobs"]["1"]["runs"] == {"1": "2024-01-01T00:00:00+00:00"}


def test_second_run_nothing_changed_makes_one_runs_listing_and_no_refetches():
    session = _orchestration_session()
    state = {}
    list(_iter_rows(session, _config(), state))

    session.calls.clear()
    rows = list(_iter_rows(session, _config(), state))

    runs_list_calls = [c for c in session.calls if urlsplit(c[0]).path.endswith("/runs/")]
    assert len(runs_list_calls) == 1  # exactly one /runs/ listing per job per pass
    assert not any("manifest.json" in c[0] for c in session.calls)
    assert not any("catalog.json" in c[0] for c in session.calls)
    assert not any(re.search(r"/runs/\d+/$", urlsplit(c[0]).path) for c in session.calls)
    assert rows == []


def test_new_successful_run_with_changed_description_only_that_row_changes():
    session1 = _orchestration_session(
        manifest_nodes={
            "model.jaffle_shop.orders": _model_node("orders", description="v1"),
            "model.jaffle_shop.customers": _model_node("customers", description="c"),
        }
    )
    state = {}
    list(_iter_rows(session1, _config(), state))

    manifest2 = _manifest(
        nodes={
            "model.jaffle_shop.orders": _model_node("orders", description="v2"),
            "model.jaffle_shop.customers": _model_node("customers", description="c"),
        }
    )
    session2 = FakeDbtCloudSession(
        jobs={1: _job(1, environment_id=10)},
        runs={
            1: _run(1, job_definition_id=1, finished_at="2024-01-01T00:00:00+00:00"),
            2: _run(2, job_definition_id=1, status=10, finished_at="2024-01-02T00:00:00+00:00"),
        },
        manifests={2: manifest2},
        run_steps={2: [{"index": 1, "name": "dbt build"}]},
        run_results={(2, 1): {"results": []}},
    )
    rows = list(_iter_rows(session2, _config(), state))
    orders_row = next(
        r for r in rows if r["id"] == _definition_doc_id(ACCOUNT_ID, 10, "model.jaffle_shop.orders")
    )
    assert "v2" in orders_row["content"]
    customers_row = next(
        r
        for r in rows
        if r["id"] == _definition_doc_id(ACCOUNT_ID, 10, "model.jaffle_shop.customers")
    )
    assert "c" in customers_row["content"]
    assert state["envs"]["10"]["manifest_run_id"] == 2


def test_node_removed_from_new_manifest_is_tombstoned_and_removed_from_state():
    session1 = _orchestration_session(
        manifest_nodes={
            "model.jaffle_shop.orders": _model_node("orders"),
            "model.jaffle_shop.customers": _model_node("customers"),
        }
    )
    state = {}
    list(_iter_rows(session1, _config(), state))

    manifest2 = _manifest(nodes={"model.jaffle_shop.orders": _model_node("orders")})
    session2 = FakeDbtCloudSession(
        jobs={1: _job(1, environment_id=10)},
        runs={
            1: _run(1, job_definition_id=1, finished_at="2024-01-01T00:00:00+00:00"),
            2: _run(2, job_definition_id=1, status=10, finished_at="2024-01-02T00:00:00+00:00"),
        },
        manifests={2: manifest2},
        run_steps={2: [{"index": 1, "name": "dbt build"}]},
        run_results={(2, 1): {"results": []}},
    )
    rows = list(_iter_rows(session2, _config(), state))
    tombstoned = [r["id"] for r in rows if r["_deleted"]]
    assert _definition_doc_id(ACCOUNT_ID, 10, "model.jaffle_shop.customers") in tombstoned
    assert "model.jaffle_shop.customers" not in str(state["envs"]["10"]["node_ids"])


def test_newer_failed_run_does_not_change_definitions():
    session1 = _orchestration_session()
    state = {}
    list(_iter_rows(session1, _config(), state))

    # session2 deliberately has NO manifest registered for run 2: if the
    # code wrongly treated the newer FAILED run as a manifest candidate,
    # fetching its manifest would 404 and raise.
    session2 = FakeDbtCloudSession(
        jobs={1: _job(1, environment_id=10)},
        runs={
            1: _run(1, job_definition_id=1, finished_at="2024-01-01T00:00:00+00:00"),
            2: _run(2, job_definition_id=1, status=20, finished_at="2024-01-02T00:00:00+00:00"),
        },
        run_steps={2: [{"index": 1, "name": "dbt build"}]},
        run_results={
            (2, 1): {
                "results": [
                    {
                        "unique_id": "model.jaffle_shop.orders",
                        "status": "error",
                        "message": "boom",
                    }
                ]
            }
        },
    )
    rows = list(_iter_rows(session2, _config(), state))
    assert state["envs"]["10"]["manifest_run_id"] == 1
    assert any(r["id"] == _run_doc_id(ACCOUNT_ID, 2) for r in rows)


def test_run_outcomes_window_slides_via_iter_rows():
    config = _config(max_runs_per_job=1)
    session1 = _orchestration_session()
    state = {}
    list(_iter_rows(session1, config, state))
    assert "1" in state["jobs"]["1"]["runs"]

    session2 = FakeDbtCloudSession(
        jobs={1: _job(1, environment_id=10)},
        runs={
            1: _run(1, job_definition_id=1, finished_at="2024-01-01T00:00:00+00:00"),
            2: _run(2, job_definition_id=1, status=10, finished_at="2024-01-02T00:00:00+00:00"),
        },
        manifests={2: _manifest(nodes={"model.jaffle_shop.orders": _model_node("orders")})},
        run_steps={2: [{"index": 1, "name": "dbt build"}]},
        run_results={(2, 1): {"results": []}},
    )
    rows = list(_iter_rows(session2, config, state))
    tombstoned = [r["id"] for r in rows if r["_deleted"] and ":run:" in r["id"]]
    assert tombstoned == [_run_doc_id(ACCOUNT_ID, 1)]
    assert state["jobs"]["1"]["runs"] == {"2": "2024-01-02T00:00:00+00:00"}


def test_config_change_forces_full_pass_and_tombstones_out_of_scope():
    source_node = {
        "name": "raw_customers",
        "resource_type": "source",
        "package_name": "jaffle_shop",
    }
    session = _orchestration_session()
    session.manifests[1] = _manifest(
        nodes={"model.jaffle_shop.orders": _model_node("orders")},
        sources={"source.jaffle_shop.ecom.raw_customers": source_node},
    )
    state = {}
    list(_iter_rows(session, _config(), state))
    assert any("source." in nid for nid in state["envs"]["10"]["node_ids"])

    rows = list(_iter_rows(session, _config(resource_types=("models",)), state))
    tombstoned = [r["id"] for r in rows if r["_deleted"]]
    assert any("source." in tid for tid in tombstoned)
    assert all("source." not in nid for nid in state["envs"]["10"]["node_ids"])


def test_vanished_job_is_tombstoned_via_iter_rows():
    # Two jobs resolve in env 10; job 2 has no runs of its own, it just
    # keeps job resolution non-empty (so the empty-resolved-set guard does
    # not trip) once job 1 disappears on the second sync.
    session1 = FakeDbtCloudSession(
        jobs={1: _job(1, environment_id=10), 2: _job(2, environment_id=10)},
        runs={1: _run(1, job_definition_id=1, finished_at="2024-01-01T00:00:00+00:00")},
        manifests={1: _manifest(nodes={"model.jaffle_shop.orders": _model_node("orders")})},
        run_steps={1: [{"index": 1, "name": "dbt build"}]},
        run_results={(1, 1): {"results": []}},
    )
    # Vanish-detection only runs on full passes, so force every pass full.
    config = _config(environment_ids=(10,), job_ids=None, full_sync_every=1)
    state = {}
    list(_iter_rows(session1, config, state))
    assert {"1", "2"} == set(state["jobs"])

    session2 = FakeDbtCloudSession(jobs={2: _job(2, environment_id=10)})
    rows = list(_iter_rows(session2, config, state))
    tombstoned = [r["id"] for r in rows if r["_deleted"]]
    assert _run_doc_id(ACCOUNT_ID, 1) in tombstoned
    assert "1" not in state["jobs"]
    assert "2" in state["jobs"]


def test_mid_run_exception_during_run_listing_leaves_state_unchanged():
    session = _orchestration_session()
    state = {}
    list(_iter_rows(session, _config(), state))
    before = json.loads(json.dumps(state))

    boom = _BoomOnPathMatch(session, r"/runs/$")
    with pytest.raises(RuntimeError, match="network boom"):
        list(_iter_rows(boom, _config(), state))
    assert state == before


def test_mid_run_exception_during_manifest_download_leaves_state_unchanged():
    session = _orchestration_session()
    state = {}
    list(_iter_rows(session, _config(), state))
    before = json.loads(json.dumps(state))

    session.runs[2] = _run(
        2, job_definition_id=1, status=10, finished_at="2024-01-02T00:00:00+00:00"
    )
    boom = _BoomOnPathMatch(session, r"manifest\.json$")
    with pytest.raises(RuntimeError, match="network boom"):
        list(_iter_rows(boom, _config(), state))
    assert state == before


def test_mid_run_exception_during_run_detail_fetch_leaves_state_unchanged():
    session = _orchestration_session()
    state = {}
    list(_iter_rows(session, _config(), state))
    before = json.loads(json.dumps(state))

    # Run 2 is NOT a success, so environment-definition sync stays a no-op
    # skip (unaffected); the failure must come from building its run-outcome
    # document, which hits `/runs/2/` for run-step detail.
    session.runs[2] = _run(
        2, job_definition_id=1, status=20, finished_at="2024-01-02T00:00:00+00:00"
    )
    boom = _BoomOnPathMatch(session, r"/runs/2/$")
    with pytest.raises(RuntimeError, match="network boom"):
        list(_iter_rows(boom, _config(), state))
    assert state == before


def test_full_sync_every_one_means_every_run_is_full():
    session = _orchestration_session()
    state = {}
    list(_iter_rows(session, _config(full_sync_every=1), state))
    assert state["runs_since_full"] == 0
    list(_iter_rows(session, _config(full_sync_every=1), state))
    assert state["runs_since_full"] == 0


def test_full_sync_every_counts_up_then_resets():
    session = _orchestration_session()
    state = {}
    list(_iter_rows(session, _config(full_sync_every=3), state))  # run 1: full (empty state)
    assert state["runs_since_full"] == 0
    list(_iter_rows(session, _config(full_sync_every=3), state))  # run 2: 0+1=1>=3? no
    assert state["runs_since_full"] == 1
    list(_iter_rows(session, _config(full_sync_every=3), state))  # run 3: 1+1=2>=3? no
    assert state["runs_since_full"] == 2
    list(_iter_rows(session, _config(full_sync_every=3), state))  # run 4: 2+1=3>=3? yes -> full
    assert state["runs_since_full"] == 0


# ---------------------------------------------------------------------------
# dlt pipeline: merge + forget-on-delete (needs dlt)
# ---------------------------------------------------------------------------


@pytest.fixture
def dlt_mod():
    return pytest.importorskip("dlt")


def _run_sync(dlt, tmp_path, run_name, session, full_sync_every=10):
    db_path = (tmp_path / f"{run_name}.db").as_posix()
    pipeline = dlt.pipeline(
        pipeline_name=run_name,
        destination=dlt.destinations.sqlalchemy(f"sqlite:///{db_path}"),
        dataset_name="dbt_cloud_ds",
        pipelines_dir=str(tmp_path / "state"),
    )
    pipeline.run(
        dbt_cloud_source(
            account_id=ACCOUNT_ID,
            environment_ids=[10],
            api_token="tok",
            full_sync_every=full_sync_every,
            session=session,
        )
    )
    return pipeline


def _read_documents(pipeline):
    with (
        pipeline.sql_client() as client,
        client.execute_query("SELECT id, title, content FROM dbt_cloud_documents") as cursor,
    ):
        rows = cursor.fetchall()
    return {row[0]: {"id": row[0], "title": row[1], "content": row[2]} for row in rows}


def test_initial_sync_stages_definitions_and_run_outcomes(dlt_mod, tmp_path):
    session = _orchestration_session()
    pipeline = _run_sync(dlt_mod, tmp_path, "dbt_cloud_initial", session)
    rows = _read_documents(pipeline)
    assert _definition_doc_id(ACCOUNT_ID, 10, "model.jaffle_shop.orders") in rows
    assert _run_doc_id(ACCOUNT_ID, 1) in rows


def test_merge_removes_tombstoned_rows_and_keeps_unchanged_rows(dlt_mod, tmp_path):
    session1 = FakeDbtCloudSession(
        jobs={1: _job(1, environment_id=10)},
        runs={1: _run(1, job_definition_id=1, finished_at="2024-01-01T00:00:00+00:00")},
        manifests={
            1: _manifest(
                nodes={
                    "model.jaffle_shop.orders": _model_node("orders"),
                    "model.jaffle_shop.customers": _model_node("customers"),
                }
            )
        },
        run_steps={1: [{"index": 1, "name": "dbt build"}]},
        run_results={(1, 1): {"results": []}},
    )
    _run_sync(dlt_mod, tmp_path, "dbt_cloud_merge", session1)

    session2 = FakeDbtCloudSession(
        jobs={1: _job(1, environment_id=10)},
        runs={
            1: _run(1, job_definition_id=1, finished_at="2024-01-01T00:00:00+00:00"),
            2: _run(2, job_definition_id=1, status=10, finished_at="2024-01-02T00:00:00+00:00"),
        },
        manifests={2: _manifest(nodes={"model.jaffle_shop.orders": _model_node("orders")})},
        run_steps={2: [{"index": 1, "name": "dbt build"}]},
        run_results={(2, 1): {"results": []}},
    )
    pipeline = _run_sync(dlt_mod, tmp_path, "dbt_cloud_merge", session2)

    rows = _read_documents(pipeline)
    assert _definition_doc_id(ACCOUNT_ID, 10, "model.jaffle_shop.orders") in rows
    assert _definition_doc_id(ACCOUNT_ID, 10, "model.jaffle_shop.customers") not in rows


def test_mid_run_exception_aborts_without_partial_commit(dlt_mod, tmp_path):
    db_path = (tmp_path / "dbt_cloud_boom.db").as_posix()
    pipeline = dlt_mod.pipeline(
        pipeline_name="dbt_cloud_boom",
        destination=dlt_mod.destinations.sqlalchemy(f"sqlite:///{db_path}"),
        dataset_name="dbt_cloud_ds",
        pipelines_dir=str(tmp_path / "state"),
    )
    with pytest.raises(Exception):  # noqa: B017 - dlt wraps the source error in PipelineStepFailed
        pipeline.run(
            dbt_cloud_source(
                account_id=ACCOUNT_ID, environment_ids=[10], api_token="tok", session=_BoomSession()
            )
        )
