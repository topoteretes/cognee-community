"""Offline tests for the PostHog connector: a fake project API with paginated listings."""

import copy

import pytest
import requests

import cognee_community_connector_posthog.posthog as ph
from cognee_community_connector_posthog.posthog import (
    PostHogAPI,
    PostHogSyncError,
    RenderContext,
    describe_properties,
    describe_query,
    describe_result,
    posthog_source,
    sync_project,
)

HOST = "https://us.posthog.com"
PROJECT = "42"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------
def _project():
    return {
        "insights": [
            {
                "id": 1,
                "short_id": "abc123",
                "name": "Weekly signups",
                "description": "New accounts per week.",
                "tags": ["growth"],
                "dashboards": [7],
                "query": {
                    "kind": "InsightVizNode",
                    "source": {
                        "kind": "TrendsQuery",
                        "series": [{"kind": "EventsNode", "event": "signed_up", "math": "total"}],
                        "breakdownFilter": {"breakdown": "$browser"},
                        "dateRange": {"date_from": "-30d"},
                    },
                },
                "result": [
                    {"label": "signed_up", "breakdown_value": "Chrome", "count": 120.0},
                    {"label": "signed_up", "breakdown_value": "Safari", "count": 45.0},
                ],
            },
            {
                "id": 2,
                "short_id": "def456",
                "name": "",
                "derived_name": "Checkout funnel",
                "dashboards": [7],
                "query": {
                    "kind": "InsightVizNode",
                    "source": {
                        "kind": "FunnelsQuery",
                        "series": [
                            {"kind": "EventsNode", "event": "viewed_cart"},
                            {"kind": "EventsNode", "event": "paid"},
                        ],
                    },
                },
                "result": [
                    {"name": "viewed_cart", "order": 0, "count": 200},
                    {"name": "paid", "order": 1, "count": 50},
                ],
            },
            {"id": 3, "short_id": "gone", "name": "Old insight", "deleted": True},
        ],
        "dashboards": [
            {"id": 7, "name": "Growth", "description": "Weekly review.", "pinned": True}
        ],
        "cohorts": [
            {
                "id": 11,
                "name": "Power users",
                "description": "Active most days.",
                "count": 312,
                "is_static": False,
                "filters": {
                    "properties": {
                        "type": "OR",
                        "values": [
                            {"key": "plan", "operator": "exact", "value": ["pro", "team"]},
                            {"key": "email", "operator": "is_set", "value": None},
                        ],
                    }
                },
            }
        ],
        "feature_flags": [
            {
                "id": 21,
                "key": "new-checkout",
                "name": "One-page checkout",
                "active": True,
                "filters": {
                    "groups": [{"properties": [], "rollout_percentage": 50}],
                    "multivariate": {
                        "variants": [
                            {"key": "control", "rollout_percentage": 50},
                            {"key": "test", "rollout_percentage": 50},
                        ]
                    },
                },
                "experiment_set": [31],
                "tags": ["checkout"],
            }
        ],
        "experiments": [
            {
                "id": 31,
                "name": "Checkout redesign",
                "description": "One page converts better than three.",
                "feature_flag_key": "new-checkout",
                "parameters": {
                    "feature_flag_variants": [
                        {"key": "control", "rollout_percentage": 50},
                        {"key": "test", "rollout_percentage": 50},
                    ]
                },
                "start_date": "2026-09-01T00:00:00Z",
                "end_date": "2026-09-30T00:00:00Z",
                "conclusion": "won",
                "conclusion_comment": "Test lifted paid conversion by 4 points.",
            }
        ],
    }


class FakeResponse:
    def __init__(self, status_code, payload=None, headers=None):
        self.status_code = status_code
        self._payload = payload if payload is not None else {}
        self.headers = headers or {}

    def json(self):
        if isinstance(self._payload, str):
            raise ValueError("not JSON")
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"HTTP {self.status_code}", response=self)


class FakePostHog:
    """Serves /api/projects/<id>/<kind>/ with limit/offset paging and absolute next links."""

    def __init__(self, data, page_size=1, project=PROJECT):
        self.data = data
        self.page_size = page_size
        self.project = project
        self.calls = []
        self.scripted = []
        self.stuck = False
        self.closed = False
        self.fail_on_call = None

    def request(self, method, url, params=None, timeout=None):
        self.calls.append((url, params))
        if self.fail_on_call == len(self.calls):
            return FakeResponse(400, {"detail": "boom"})
        if self.scripted:
            return self.scripted.pop(0)
        path, _, query = url.partition("?")
        kind = path.rstrip("/").split("/")[-1]
        assert path.startswith(f"{HOST}/api/projects/{self.project}/"), path
        if kind not in self.data:
            return FakeResponse(404, {"detail": "Not found."})
        offset = int(dict(p.split("=") for p in query.split("&")).get("offset", 0)) if query else 0
        if kind == "insights" and params is not None:
            assert params["saved"] == "true" and params["refresh"] == "force_cache"
        items = self.data[kind][offset : offset + self.page_size]
        more = offset + self.page_size < len(self.data[kind])
        next_url = None
        if more or self.stuck:
            step = 0 if self.stuck else self.page_size
            next_url = f"{path}?limit={self.page_size}&offset={offset + step}"
        return FakeResponse(
            200, {"count": len(self.data[kind]), "next": next_url, "results": copy.deepcopy(items)}
        )

    def close(self):
        self.closed = True


def _api(backend):
    return PostHogAPI(backend, HOST, PROJECT)


def _rows(backend, state, kinds=ph.DEFAULT_KINDS, **kwargs):
    return list(sync_project(_api(backend), kinds, state, **kwargs))


def _by_id(rows):
    return {row["id"]: row for row in rows}


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------
def test_insight_document_has_the_query_in_words_and_its_cached_result():
    rows = _by_id(_rows(FakePostHog(_project()), {}))
    insight = rows["insight:1"]

    assert insight["title"] == "Insight: Weekly signups"
    assert insight["url"] == f"{HOST}/project/{PROJECT}/insights/abc123"
    assert insight["content"].splitlines() == [
        "PostHog insight Weekly signups.",
        "New accounts per week.",
        "Query: Trends of signed_up broken down by $browser from -30d.",
        "Tags: growth.",
        "On dashboards: Growth.",
        "Latest cached result:",
        "signed_up (Chrome): 120.",
        "signed_up (Safari): 45.",
    ]


def test_funnel_result_lists_steps_and_conversion():
    rows = _by_id(_rows(FakePostHog(_project()), {}))
    funnel = rows["insight:2"]["content"].splitlines()

    assert funnel[0] == "PostHog insight Checkout funnel."  # derived name when unnamed
    assert "Query: Funnel of viewed_cart then paid." in funnel
    assert funnel[-3:] == [
        "Step 1, viewed_cart: 200.",
        "Step 2, paid: 50.",
        "Overall conversion: 25.0%.",
    ]


def test_dashboard_cohort_flag_and_experiment_documents():
    rows = _by_id(_rows(FakePostHog(_project()), {}))

    assert rows["dashboard:7"]["content"].splitlines() == [
        "PostHog dashboard Growth.",
        "Weekly review.",
        "Pinned.",
        "Insights on it: Checkout funnel, Weekly signups.",
    ]
    assert rows["cohort:11"]["content"].splitlines() == [
        "PostHog cohort Power users.",
        "Active most days.",
        "Dynamic cohort.",
        "Size: 312 people.",
        "Criteria: (plan exact pro, team or email is set).",
    ]
    assert rows["feature_flag:21"]["content"].splitlines() == [
        "PostHog feature flag new-checkout.",
        "One-page checkout",
        "Status: active.",
        "Release condition: 50% of everyone.",
        "Variants: control 50%, test 50%.",
        "Used by experiments: Checkout redesign.",
        "Tags: checkout.",
    ]
    assert rows["experiment:31"]["content"].splitlines() == [
        "PostHog experiment Checkout redesign.",
        "One page converts better than three.",
        "Feature flag: new-checkout.",
        "Variants: control 50%, test 50%.",
        "Ran from 2026-09-01 to 2026-09-30.",
        "Conclusion: won.",
        "Test lifted paid conversion by 4 points.",
    ]
    assert rows["feature_flag:21"]["url"] == f"{HOST}/project/{PROJECT}/feature_flags/21"


def test_query_and_property_rendering_edge_cases():
    sql = {"kind": "DataVisualizationNode", "source": {"kind": "HogQLQuery", "query": "select 1"}}
    assert describe_query(sql) == "SQL query: select 1"
    assert describe_query(None) == ""
    assert describe_result({"source": {"kind": "RetentionQuery"}}, [{"x": 1}]) == []
    assert describe_result(None, None) == []
    assert describe_properties([{"key": "country", "operator": "is_not", "value": "US"}]) == (
        "country is not US"
    )
    assert describe_properties({"type": "cohort", "key": "id", "value": 5}) == "in cohort 5"


# ---------------------------------------------------------------------------
# Source configuration
# ---------------------------------------------------------------------------
def test_source_declares_document_marker_scope_and_validates():
    from cognee.tasks.ingestion.dlt_utils import DOCUMENT_SOURCE_ATTR, PIPELINE_SCOPE_ATTR

    source = posthog_source(project_id=42, host="https://eu.posthog.com/", service=FakePostHog({}))
    assert getattr(source, DOCUMENT_SOURCE_ATTR) == "posthog"
    # The table and the state scope share one name, derived from the project.
    assert getattr(source, PIPELINE_SCOPE_ATTR) == "posthog_42"
    assert list(source.resources) == ["posthog_42"]
    named = posthog_source(project_id=42, service=FakePostHog({}), resource_name="ph_prod")
    assert getattr(named, PIPELINE_SCOPE_ATTR) == "ph_prod"

    with pytest.raises(ValueError, match="api_key is required"):
        posthog_source(project_id=42)
    with pytest.raises(ValueError, match="project number"):
        posthog_source(project_id="my-project", api_key="k")
    with pytest.raises(ValueError, match="project number"):
        posthog_source(project_id=None, api_key="k")
    with pytest.raises(ValueError, match="host must be a URL"):
        posthog_source(project_id=42, api_key="k", host="eu.posthog.com")
    with pytest.raises(ValueError, match="kinds"):
        posthog_source(project_id=42, api_key="k", kinds=["persons"])
    with pytest.raises(ValueError, match="list of kind names"):
        posthog_source(project_id=42, api_key="k", kinds="insights")
    with pytest.raises(ValueError, match="snake_case"):
        posthog_source(project_id=42, api_key="k", resource_name="PostHog-Prod")
    with pytest.raises(ValueError, match="snake_case"):
        posthog_source(project_id=42, api_key="k", resource_name="posthog__42")
    with pytest.raises(ValueError, match="not both"):
        posthog_source(project_id=42, service=FakePostHog({}), session=FakePostHog({}))


def test_source_refuses_a_cognee_without_document_sync(monkeypatch):
    monkeypatch.delattr(ph.dlt_utils, "DOCUMENT_SYNC_VERSION")
    with pytest.raises(RuntimeError, match="document sync cleanup"):
        posthog_source(project_id=42, api_key="k")


def test_api_key_is_stripped_sent_as_bearer_and_never_echoed():
    assert ph._check_key(" phx_abc\n") == "phx_abc"
    assert ph._make_session("phx_abc").headers["Authorization"] == "Bearer phx_abc"
    with pytest.raises(ValueError) as error:
        ph._check_key("phx abc")
    assert "phx" not in str(error.value)


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------
def test_listing_follows_next_links_and_skips_deleted_objects():
    backend = FakePostHog(_project(), page_size=1)
    rows = _rows(backend, {}, kinds=["insights"])

    assert sorted(row["id"] for row in rows) == ["insight:1", "insight:2"]  # #3 is deleted
    assert len(backend.calls) == 3


def test_repeated_next_link_stops_the_run():
    backend = FakePostHog(_project(), page_size=1)
    backend.stuck = True

    with pytest.raises(PostHogSyncError, match="same insights page link"):
        list(_api(backend).list("insights"))


def test_retries_honour_retry_after_within_the_wait_budget(monkeypatch):
    slept = []
    monkeypatch.setattr(ph.time, "sleep", slept.append)
    backend = FakePostHog({"cohorts": []})

    backend.scripted = [FakeResponse(429, headers={"Retry-After": "600"})]
    assert list(_api(backend).list("cohorts")) == []
    assert slept == [60.0]  # Retry-After is capped at a minute

    slept.clear()
    backend.scripted = [FakeResponse(502), FakeResponse(503)]
    assert list(_api(backend).list("cohorts")) == []
    assert slept == [1.0, 2.0]

    slept.clear()
    backend.scripted = [FakeResponse(500)] * 3
    with pytest.raises(PostHogSyncError, match="HTTP 500"):
        list(_api(backend).list("cohorts"))
    assert slept == [1.0, 2.0]  # three attempts, then the run stops


def test_rate_limit_stops_once_the_wait_budget_is_spent(monkeypatch):
    slept = []
    monkeypatch.setattr(ph.time, "sleep", slept.append)
    backend = FakePostHog({"cohorts": []})
    backend.scripted = [FakeResponse(429, headers={"Retry-After": "45"})] * 3
    api = _api(backend)

    with pytest.raises(PostHogSyncError, match="rate-limiting"):
        list(api.list("cohorts"))
    assert slept == [45.0]  # a second 45 s wait would pass the 60 s budget
    assert api.stats["failed_rate_limit"] == 1


def test_auth_errors_name_the_scopes_and_are_not_retried(monkeypatch):
    slept = []
    monkeypatch.setattr(ph.time, "sleep", slept.append)
    backend = FakePostHog({"cohorts": []})
    backend.scripted = [FakeResponse(403)]
    api = _api(backend)

    with pytest.raises(requests.HTTPError, match="read scopes"):
        list(api.list("cohorts"))
    assert slept == []
    assert api.stats["failed_auth"] == 1


def test_wrong_project_and_web_pages_are_explained():
    backend = FakePostHog({})
    with pytest.raises(PostHogSyncError, match="project_id"):
        list(_api(backend).list("cohorts"))
    backend.scripted = [FakeResponse(200, "<html>PostHog</html>")]
    with pytest.raises(PostHogSyncError, match="web page"):
        list(_api(backend).list("cohorts"))


# ---------------------------------------------------------------------------
# Sync
# ---------------------------------------------------------------------------
def test_rerun_without_changes_emits_nothing():
    backend = FakePostHog(_project())
    state = {}
    assert len(_rows(backend, state)) == 6
    assert _rows(backend, state) == []


def test_new_cached_result_and_renames_re_emit_only_the_affected_documents():
    backend = FakePostHog(_project())
    state = {}
    _rows(backend, state)

    backend.data["insights"][0]["result"][0]["count"] = 130.0
    assert [row["id"] for row in _rows(backend, state)] == ["insight:1"]

    backend.data["dashboards"][0]["name"] = "Growth review"  # named by both insights
    rows = _rows(backend, state)
    assert sorted(row["id"] for row in rows) == ["dashboard:7", "insight:1", "insight:2"]


def test_deleted_or_vanished_objects_are_tombstoned():
    backend = FakePostHog(_project())
    state = {}
    _rows(backend, state)

    backend.data["feature_flags"][0]["deleted"] = True
    del backend.data["cohorts"][0]
    rows = _rows(backend, state)

    assert {"id": "feature_flag:21", "_deleted": True} in rows
    assert {"id": "cohort:11", "_deleted": True} in rows


def test_kind_dropped_from_the_selection_is_tombstoned():
    backend = FakePostHog(_project())
    state = {}
    _rows(backend, state)

    rows = _rows(backend, state, kinds=["insights", "dashboards", "cohorts", "feature_flags"])

    assert {"id": "experiment:31", "_deleted": True} in rows
    assert any(row["id"] == "feature_flag:21" for row in rows)  # names the experiment by id now


def test_empty_listing_for_the_same_selection_forgets_nothing():
    backend = FakePostHog(_project())
    state = {}
    _rows(backend, state)
    before = copy.deepcopy(state)

    backend.data = {kind: [] for kind in backend.data}  # e.g. a key for another project
    with pytest.raises(PostHogSyncError, match="Refusing to forget"):
        _rows(backend, state)
    assert state == before


def test_objects_without_an_id_are_skipped_and_counted():
    data = _project()
    data["cohorts"].append({"name": "broken"})
    stats = {}

    rows = _rows(FakePostHog(data), {}, kinds=["cohorts"], stats=stats)

    assert [row["id"] for row in rows] == ["cohort:11"]
    assert stats["failed"] == 1


def test_stats_and_check_active():
    backend = FakePostHog(_project(), page_size=10)
    stats, calls = {}, []
    _rows(backend, {}, stats=stats, check_active=lambda: calls.append(1))
    assert stats == {
        "scanned": 6,
        "skipped": 0,
        "changed": 6,
        "deleted": 0,
        "failed": 0,
        "failed_auth": 0,
        "failed_rate_limit": 0,
        "requests": 5,  # one page per kind
        "complete": 1,
    }
    assert len(calls) == 6  # once per published row (the API client was built without it)

    state = {}

    def revoked():
        raise PermissionError("revoked")

    with pytest.raises(PermissionError):
        list(
            sync_project(
                PostHogAPI(backend, HOST, PROJECT, check_active=revoked), ["cohorts"], state
            )
        )
    assert state.get("documents", {}) == {}


def test_failed_page_aborts_before_any_row_or_state_change():
    backend = FakePostHog(_project())
    state = {}
    _rows(backend, state)
    before = copy.deepcopy(state)

    del backend.data["cohorts"][0]
    backend.calls.clear()
    backend.fail_on_call = 4  # a later listing fails after the cohorts were read
    with pytest.raises(requests.HTTPError):
        _rows(backend, state)
    assert state == before  # the vanished cohort is not tombstoned


def test_render_context_urls_quote_the_project():
    context = RenderContext(HOST, "a/b")
    assert context.url("cohorts", 5) == f"{HOST}/project/a%2Fb/cohorts/5"


# ---------------------------------------------------------------------------
# End to end through dlt merge (what cognee.remember does)
# ---------------------------------------------------------------------------
@pytest.fixture
def dlt_mod():
    return pytest.importorskip("dlt")


def _pipeline(dlt, tmp_path, name):
    return dlt.pipeline(
        pipeline_name=name,
        destination=dlt.destinations.sqlalchemy(
            f"sqlite:///{(tmp_path / f'{name}.db').as_posix()}"
        ),
        dataset_name="posthog_ds",
        pipelines_dir=str(tmp_path / "state"),
    )


def _read(pipeline, table=f"posthog_{PROJECT}"):
    with (
        pipeline.sql_client() as client,
        client.execute_query(f"SELECT id, content FROM {table}") as cursor,
    ):
        return {row[0]: row[1] for row in cursor.fetchall()}


def test_dlt_merge_applies_changes_and_deletions(dlt_mod, tmp_path):
    backend = FakePostHog(_project())
    pipeline = _pipeline(dlt_mod, tmp_path, "posthog_test")
    pipeline.run(
        posthog_source(project_id=PROJECT, service=backend),
        write_disposition="merge",
        primary_key="id",
    )
    assert len(_read(pipeline)) == 6

    backend.data["experiments"][0]["conclusion"] = "lost"
    del backend.data["cohorts"][0]
    pipeline.run(
        posthog_source(project_id=PROJECT, service=backend),
        write_disposition="merge",
        primary_key="id",
    )
    rows = _read(pipeline)

    assert "cohort:11" not in rows
    assert "Conclusion: lost." in rows["experiment:31"]
    assert len(rows) == 5


def test_source_closes_the_session_it_opens_and_reports_stats(dlt_mod, tmp_path, monkeypatch):
    backend = FakePostHog(_project())
    monkeypatch.setattr(ph, "_make_session", lambda key: backend)
    pipeline = _pipeline(dlt_mod, tmp_path, "posthog_own")
    source = posthog_source(project_id=PROJECT, api_key="phx_key")

    pipeline.run(source, write_disposition="merge", primary_key="id")

    assert backend.closed
    assert source.cognee_sync_stats["complete"] == 1
    assert source.cognee_sync_stats["changed"] == 6


def test_two_projects_in_one_dataset_keep_their_own_table_and_state(dlt_mod, tmp_path):
    from cognee.tasks.ingestion.dlt_utils import pipeline_name_for_source

    first = FakePostHog(_project())
    second = FakePostHog(_project(), project="43")  # same object ids, other project

    def sync(backend, project):
        source = posthog_source(project_id=project, service=backend)
        # Name the pipeline as cognee does: one per (dataset, scope).
        name = pipeline_name_for_source(source, "posthog")
        pipeline = _pipeline(dlt_mod, tmp_path, name)
        pipeline.run(source, write_disposition="merge", primary_key="id")
        return pipeline

    sync(first, PROJECT)
    sync(second, "43")
    del first.data["cohorts"][0]
    pipeline = sync(first, PROJECT)
    other = sync(second, "43")

    assert "cohort:11" not in _read(pipeline)
    assert "cohort:11" in _read(other, table="posthog_43")
    assert len(_read(other, table="posthog_43")) == 6


@pytest.mark.parametrize("status", [401, 403, 404, 429, 500])
def test_the_api_key_never_reaches_errors_or_logs(
    dlt_mod, tmp_path, monkeypatch, caplog, capsys, status
):
    from dlt.pipeline.exceptions import PipelineStepFailed

    key = "phx_LeakCanary0123456789"
    monkeypatch.setattr(ph.time, "sleep", lambda seconds: None)
    backend = FakePostHog(_project())
    echo = {"detail": f"Invalid key {key}", "attr": key}  # as if PostHog echoed it back
    backend.scripted = [FakeResponse(status, echo, headers={"Retry-After": "1"})] * 3
    monkeypatch.setattr(ph, "_make_session", lambda api_key: backend)
    source = posthog_source(project_id=PROJECT, api_key=f" {key}\n")

    with pytest.raises(PipelineStepFailed) as failure:
        _pipeline(dlt_mod, tmp_path, f"posthog_leak_{status}").run(
            source, write_disposition="merge", primary_key="id"
        )

    chain, error = [], failure.value
    while error is not None:
        chain.append(f"{error!r} {error}")
        error = error.__cause__ or error.__context__
    output = capsys.readouterr()
    assert key not in " ".join(chain)
    assert key not in caplog.text + output.out + output.err
    assert source.cognee_sync_stats["complete"] == 0
    assert source.cognee_sync_stats["failed_auth"] == (1 if status in (401, 403) else 0)
    assert source.cognee_sync_stats["failed_rate_limit"] == (1 if status == 429 else 0)


def test_control_characters_and_lone_surrogates_are_dropped(dlt_mod, tmp_path):
    data = _project()
    data["dashboards"][0]["name"] = "Gro\x00wth\x1b"
    data["dashboards"][0]["description"] = "Weekly \ud800review."
    pipeline = _pipeline(dlt_mod, tmp_path, "posthog_control")

    pipeline.run(
        posthog_source(project_id=PROJECT, service=FakePostHog(data)),
        write_disposition="merge",
        primary_key="id",
    )

    assert _read(pipeline)["dashboard:7"].splitlines()[:2] == [
        "PostHog dashboard Growth.",
        "Weekly review.",
    ]
    assert _by_id(_rows(FakePostHog(data), {}))["dashboard:7"]["title"] == "Dashboard: Growth"
