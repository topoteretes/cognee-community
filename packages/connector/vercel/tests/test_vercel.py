"""Unit tests for the Vercel dlt connector.

Everything runs offline. A fake ``requests`` transport adapter stands in for
api.vercel.com, so the real ``rest_api`` source, paginator and auth run against
in-memory fixtures. Two layers:

* DB-free tests for the row builders (field allowlist, log joining).
* dlt-pipeline tests into a temp sqlite destination covering the acceptance
  criteria: nothing secret reaches staging, a re-sync reflects state changes,
  deleted deployments drop out of the snapshot, and a failed or malformed read
  aborts instead of emptying staging.
"""

import gzip
import json
import sqlite3
import time
from urllib.parse import parse_qs, urlsplit

import pytest
import requests
from requests.adapters import BaseAdapter

from cognee_community_connector_vercel.vercel import (
    _PARENT_NAME,
    _PARENT_UID,
    _PARENT_URL,
    VERCEL_BUILD_LOGS_TABLE,
    VERCEL_DEPLOYMENTS_TABLE,
    VERCEL_PROJECTS_TABLE,
    VERCEL_SOURCE_NAME,
    _build_log_rows,
    _deployment_row,
    _project_row,
    _timestamp,
    vercel_source,
)

# Values that must never leave the process. Each sits in a field the real API
# returns next to the fields the connector does want.
CANARY_ENV = "CANARY-ENV-VALUE-0001"
CANARY_HOOK = "CANARYHOOKSECRET0002"
CANARY_BYPASS = "CANARYBYPASSSECRET0003"
CANARY_META = "CANARY-META-VALUE-0004"
CANARY_EMAIL = "canary-0005@example.com"
CANARIES = (CANARY_ENV, CANARY_HOOK, CANARY_BYPASS, CANARY_META, CANARY_EMAIL)

NOW_MS = int(time.time() * 1000)
MINUTE_MS = 60_000
DAY_MS = 24 * 60 * MINUTE_MS

# ---------------------------------------------------------------------------
# Fixtures / fakes
# ---------------------------------------------------------------------------


def _project(project_id, name):
    """A project shaped like the real list response, secrets included."""
    return {
        "id": project_id,
        "name": name,
        "accountId": "team_1",
        "framework": "nextjs",
        "nodeVersion": "22.x",
        "rootDirectory": "apps/web",
        "createdAt": NOW_MS - 90 * DAY_MS,
        "updatedAt": NOW_MS,
        "env": [
            {"id": "env_1", "key": "API_KEY", "type": "plain", "value": CANARY_ENV},
            {"id": "env_2", "key": "DB_URL", "type": "sensitive", "value": CANARY_ENV},
        ],
        "link": {
            "type": "github",
            "org": "acme",
            "repo": name,
            "productionBranch": "main",
            "gitCredentialId": "cred_1",
            "deployHooks": [
                {
                    "id": "hook_1",
                    "name": "nightly",
                    "ref": "main",
                    "url": f"https://api.vercel.com/v1/integrations/deploy/{project_id}/{CANARY_HOOK}",
                }
            ],
        },
        "protectionBypass": {CANARY_BYPASS: {"scope": "automation-bypass"}},
        "latestDeployments": [{"id": "dpl_latest", "meta": {"token": CANARY_META}}],
    }


def _deployment(uid, project, state="READY", minutes_ago=10, target="production", **extra):
    """A deployment shaped like one item of the real list response."""
    created = NOW_MS - minutes_ago * MINUTE_MS
    deployment = {
        "uid": uid,
        "name": project,
        "projectId": f"prj_{project}",
        "url": f"{project}-{uid}.vercel.app",
        "inspectorUrl": f"https://vercel.com/acme/{project}/{uid}",
        "state": state,
        "readyState": state,
        "target": target,
        "created": created,
        "createdAt": created,
        "buildingAt": created + 1000,
        "ready": created + 60_000,
        "creator": {"uid": "user_1", "username": "ada", "email": CANARY_EMAIL},
        "meta": {
            "githubCommitSha": f"sha-{uid}",
            "githubCommitRef": "main",
            "githubCommitMessage": f"change for {uid}",
            "githubCommitAuthorLogin": "ada",
            "internalToken": CANARY_META,
        },
        "projectSettings": {"commandForIgnoringBuildStep": CANARY_META},
    }
    if state == "ERROR":
        deployment["errorCode"] = "BUILD_FAILED"
        deployment["errorMessage"] = 'Command "npm run build" exited with 1'
    deployment.update(extra)
    return deployment


def _log(*lines):
    """Build events in the shape the events endpoint returns."""
    return [
        {"type": "stdout", "text": line, "created": NOW_MS + index, "serial": str(index)}
        for index, line in enumerate(lines)
    ]


class FakeVercel(BaseAdapter):
    """In-memory stand-in for api.vercel.com, mounted on a real requests.Session.

    It mirrors the parts of the API the connector relies on: list bodies are
    objects with a ``pagination`` block, pages run newest first, and
    ``pagination.next`` comes back as ``until``.
    """

    def __init__(self, projects, deployments, logs=None, page_size=2):
        super().__init__()
        self.projects = projects
        self.deployments = deployments
        self.logs = logs or {}
        self.page_size = page_size
        self.calls = []
        self.fail = None  # callable(path, params) -> status code to return, or None
        self.projects_as_array = False

    def session(self):
        session = requests.Session()
        session.mount("https://", self)
        return session

    def send(self, request, **kwargs):
        url = urlsplit(request.url)
        params = {key: values[0] for key, values in parse_qs(url.query).items()}
        self.calls.append((url.path, params, request.headers.get("Authorization")))

        status = self.fail(url.path, params) if self.fail else None
        if status:
            return self._response(request, status, {"error": {"code": "boom"}})
        if url.path == "/v10/projects":
            page = self._page(self.projects, "updatedAt", "projects", params)
            body = page["projects"] if self.projects_as_array else page
            return self._response(request, 200, body)
        if url.path == "/v7/deployments":
            wanted_states = set(filter(None, params.get("state", "").split(",")))
            since = int(params.get("since", 0))
            items = [
                item
                for item in self.deployments
                if item["created"] >= since
                and (not wanted_states or item["state"] in wanted_states)
            ]
            return self._response(request, 200, self._page(items, "created", "deployments", params))
        if url.path.startswith("/v3/deployments/") and url.path.endswith("/events"):
            uid = url.path.split("/")[3]
            if uid not in self.logs:
                return self._response(request, 404, {"error": {"code": "not_found"}})
            return self._response(request, 200, self.logs[uid])
        return self._response(request, 404, {"error": {"code": "not_found"}})

    def _page(self, items, order_key, list_key, params):
        ordered = sorted(items, key=lambda item: item[order_key], reverse=True)
        if "until" in params:
            ordered = [item for item in ordered if item[order_key] < int(params["until"])]
        page = ordered[: self.page_size]
        more = len(ordered) > self.page_size
        return {
            list_key: page,
            "pagination": {"count": len(page), "next": page[-1][order_key] if more else None},
        }

    @staticmethod
    def _response(request, status, body):
        response = requests.Response()
        response.status_code = status
        response.request = request
        response.url = request.url
        response.headers["Content-Type"] = "application/json"
        response._content = json.dumps(body).encode()
        return response

    def close(self):
        pass

    def event_paths(self):
        return [path for path, _, _ in self.calls if path.endswith("/events")]


def _fleet():
    """Two projects, three deployments (one failed), one build log."""
    return FakeVercel(
        projects=[_project("prj_web", "web"), _project("prj_api", "api")],
        deployments=[
            _deployment("dpl_ok1", "web", minutes_ago=30),
            _deployment("dpl_ok2", "api", minutes_ago=20),
            _deployment("dpl_err", "web", state="ERROR", minutes_ago=10),
        ],
        logs={"dpl_err": _log("Installing dependencies", "Error: module not found")},
    )


# ---------------------------------------------------------------------------
# Row builders (DB-free)
# ---------------------------------------------------------------------------


def test_project_row_keeps_only_allowlisted_fields():
    row = _project_row(_project("prj_web", "web"))

    assert set(row) == {"id", "title", "content"}
    assert row["id"] == "prj_web"
    assert row["title"] == "Vercel project web"
    assert "Framework: nextjs" in row["content"]
    assert "Git repository: github: acme/web" in row["content"]
    assert "Production branch: main" in row["content"]
    # env values, the deploy-hook URL, bypass secrets and embedded deployments
    # all sit on the same object and must not be copied.
    assert not any(canary in json.dumps(row) for canary in CANARIES)
    assert "API_KEY" not in row["content"]


def test_project_row_ignores_volatile_updated_at():
    # updatedAt moves without any change to the fields that are kept; if it leaked
    # into the row every sync would change the content hash.
    first = _project("prj_web", "web")
    second = {**first, "updatedAt": first["updatedAt"] + DAY_MS}
    assert _project_row(first) == _project_row(second)


def test_deployment_row_keeps_status_commit_author_and_timing():
    row = _deployment_row(_deployment("dpl_err", "web", state="ERROR"))

    assert set(row) == {"id", "title", "content", "url"}
    assert row["id"] == "dpl_err"
    assert row["url"] == "https://vercel.com/acme/web/dpl_err"
    for expected in (
        "Project: web",
        "Target: production",
        "State: ERROR",
        "Created by: ada",
        "Commit: sha-dpl_err",
        "Branch: main",
        "Commit message: change for dpl_err",
        'Error: BUILD_FAILED: Command "npm run build" exited with 1',
    ):
        assert expected in row["content"]
    # The rest of meta, projectSettings and the creator's email stay behind.
    assert not any(canary in json.dumps(row) for canary in CANARIES)


def test_deployment_row_does_not_call_a_failed_build_ready():
    # A failed deployment carries a `ready` timestamp as well.
    content = _deployment_row(_deployment("dpl_err", "web", state="ERROR"))["content"]
    assert "Finished: " in content
    assert "Ready" not in content


def test_deployment_row_reads_commit_from_any_git_provider():
    deployment = _deployment("dpl_1", "web")
    deployment["meta"] = {"gitlabCommitSha": "abc123", "gitlabCommitRef": "develop"}
    content = _deployment_row(deployment)["content"]
    assert "Commit: abc123" in content
    assert "Branch: develop" in content


def test_deployment_row_names_preview_when_target_is_null():
    row = _deployment_row(_deployment("dpl_1", "web", target=None))
    assert "Target: preview" in row["content"]
    assert row["title"] == "Vercel deployment dpl_1 of web (preview)"


def test_timestamp_is_stable_utc_text():
    assert _timestamp(0) == "1970-01-01 00:00 UTC"
    assert _timestamp(1_700_000_000_000) == "2023-11-14 22:13 UTC"
    assert _timestamp(None) is None
    assert _timestamp("soon") is None


def _events(uid, *lines, name="web"):
    """Child rows as rest_api hands them to the transformer: parent fields attached."""
    return [
        {**event, _PARENT_UID: uid, _PARENT_NAME: name, _PARENT_URL: f"https://vercel.com/{uid}"}
        for event in _log(*lines)
    ]


def test_build_log_rows_join_lines_in_order():
    rows = list(_build_log_rows(_events("dpl_err", "first", "second"), max_log_chars=None))

    assert rows == [
        {
            "id": "dpl_err",
            "title": "Build output of failed Vercel deployment dpl_err of web",
            "content": "first\nsecond",
            "url": "https://vercel.com/dpl_err",
        }
    ]


def test_build_log_rows_keep_the_tail_when_capped():
    events = _events("dpl_err", "a" * 50, "the real error")
    (row,) = _build_log_rows(events, max_log_chars=14)

    # The end of a build log is where the failure is, so the cap drops the start.
    assert row["content"] == "[earlier output truncated]\nthe real error"


def test_build_log_rows_skip_deployments_without_text():
    events = [{"type": "delimiter", _PARENT_UID: "dpl_err", _PARENT_NAME: "web"}]
    assert list(_build_log_rows(events, max_log_chars=None)) == []


def test_build_log_rows_read_payload_text_shape():
    events = [{"payload": {"text": "from payload\n"}, _PARENT_UID: "dpl_err", _PARENT_NAME: "web"}]
    (row,) = _build_log_rows(events, max_log_chars=None)
    assert row["content"] == "from payload"


def test_build_log_rows_keep_deployments_apart():
    events = _events("dpl_a", "log a") + _events("dpl_b", "log b")
    rows = {row["id"]: row["content"] for row in _build_log_rows(events, max_log_chars=None)}
    assert rows == {"dpl_a": "log a", "dpl_b": "log b"}


# ---------------------------------------------------------------------------
# Source construction
# ---------------------------------------------------------------------------


def test_vercel_source_declares_document_marker():
    # resolve_dlt_sources routes on this marker; the tag it carries is the source name.
    from cognee.tasks.ingestion.dlt_utils import document_source_tag

    source = vercel_source(token="test-token")
    assert VERCEL_SOURCE_NAME == "vercel"
    assert document_source_tag(source) == "vercel"


def test_vercel_source_requires_a_token(monkeypatch):
    monkeypatch.delenv("VERCEL_TOKEN", raising=False)
    with pytest.raises(ValueError, match="VERCEL_TOKEN"):
        vercel_source()


def test_vercel_source_selects_only_loadable_resources():
    # The failed-deployments parent and the per-line events are plumbing: if they
    # were loaded, cognee would read them back as documents.
    assert set(vercel_source(token="test-token").resources.selected) == {
        VERCEL_PROJECTS_TABLE,
        VERCEL_DEPLOYMENTS_TABLE,
        VERCEL_BUILD_LOGS_TABLE,
    }
    without_logs = vercel_source(token="test-token", include_build_logs=False)
    assert set(without_logs.resources.selected) == {
        VERCEL_PROJECTS_TABLE,
        VERCEL_DEPLOYMENTS_TABLE,
    }


# ---------------------------------------------------------------------------
# dlt pipeline: snapshot sync, forget-on-delete, fail closed
# ---------------------------------------------------------------------------


@pytest.fixture
def dlt_mod():
    return pytest.importorskip("dlt")


def _pipeline(dlt, tmp_path):
    db_path = (tmp_path / "vercel.db").as_posix()
    return dlt.pipeline(
        pipeline_name="vercel_test",
        destination=dlt.destinations.sqlalchemy(f"sqlite:///{db_path}"),
        dataset_name="vercel_ds",
        pipelines_dir=str(tmp_path / "state"),
    )


def _run_sync(dlt, tmp_path, fake, **source_kwargs):
    """Run vercel_source through a dlt pipeline into a temp sqlite destination."""
    pipeline = _pipeline(dlt, tmp_path)
    pipeline.run(vercel_source(token="test-token", session=fake.session(), **source_kwargs))
    return pipeline


def _read(pipeline, table):
    """Return {id: {"title": ..., "content": ...}} for one staging table."""
    with (
        pipeline.sql_client() as client,
        client.execute_query(f"SELECT id, title, content FROM {table}") as cursor,
    ):
        rows = cursor.fetchall()
    return {row[0]: {"title": row[1], "content": row[2]} for row in rows}


def _everything_written(tmp_path):
    """Return (staging tables, text of every byte the pipeline left on disk).

    dlt's sqlite destination keeps the dataset in its own ``<db>__<dataset>.db``
    file and leaves completed load files in the pipeline working directory, so
    all of it is read: every cell of every table plus every other file.
    """
    tables = []
    written = []
    for path in sorted(tmp_path.rglob("*")):
        if not path.is_file():
            continue
        if path.suffix == ".db":
            connection = sqlite3.connect(path.as_posix())
            try:
                names = connection.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                ).fetchall()
                for (name,) in names:
                    tables.append(name)
                    cells = connection.execute(f'SELECT * FROM "{name}"').fetchall()
                    written.append(json.dumps(cells, default=str))
            finally:
                connection.close()
            continue
        raw = path.read_bytes()
        if path.suffix == ".gz":
            raw = gzip.decompress(raw)
        written.append(raw.decode("utf-8", errors="ignore"))
    return tables, "\n".join(written)


def test_first_sync_loads_projects_deployments_and_failed_build_output(dlt_mod, tmp_path):
    fake = _fleet()
    pipeline = _run_sync(dlt_mod, tmp_path, fake)

    assert set(_read(pipeline, VERCEL_PROJECTS_TABLE)) == {"prj_web", "prj_api"}
    deployments = _read(pipeline, VERCEL_DEPLOYMENTS_TABLE)
    assert set(deployments) == {"dpl_ok1", "dpl_ok2", "dpl_err"}
    assert "State: ERROR" in deployments["dpl_err"]["content"]
    logs = _read(pipeline, VERCEL_BUILD_LOGS_TABLE)
    assert set(logs) == {"dpl_err"}
    assert logs["dpl_err"]["content"] == "Installing dependencies\nError: module not found"


def test_events_are_requested_only_for_failed_deployments(dlt_mod, tmp_path):
    fake = _fleet()
    _run_sync(dlt_mod, tmp_path, fake)

    # Successful builds must never cost an events request (or have their log read).
    assert fake.event_paths() == ["/v3/deployments/dpl_err/events"]
    (events_call,) = [call for call in fake.calls if call[0].endswith("/events")]
    assert events_call[1]["limit"] == "-1"


def test_lists_are_paged_with_until_and_sent_with_bearer_auth(dlt_mod, tmp_path):
    fake = _fleet()  # page_size=2, so three deployments need a second page
    _run_sync(dlt_mod, tmp_path, fake, team_id="team_1")

    deployment_pages = [
        params
        for path, params, _ in fake.calls
        if path == "/v7/deployments" and "state" not in params
    ]
    assert len(deployment_pages) == 2
    assert "until" not in deployment_pages[0]
    # The second request carries the first page's pagination.next as `until`.
    oldest_on_page_one = sorted(item["created"] for item in fake.deployments)[1]
    assert deployment_pages[1]["until"] == str(oldest_on_page_one)
    # Every request is authenticated, team-scoped and bounded by the lookback window.
    assert {auth for _, _, auth in fake.calls} == {"Bearer test-token"}
    assert {params.get("teamId") for _, params, _ in fake.calls} == {"team_1"}
    assert all("since" in params for params in deployment_pages)


def test_no_secret_reaches_staging(dlt_mod, tmp_path):
    # The issue's hard requirement: environment variable values must never reach
    # the graph. Everything cognee reads comes out of dlt's staging, so scan all
    # of it: every table, dlt's bookkeeping tables and its load files on disk.
    fake = _fleet()
    fake.logs["dpl_err"] = _log("build failed")
    _run_sync(dlt_mod, tmp_path, fake)

    tables, written = _everything_written(tmp_path)
    assert not any(canary in written for canary in CANARIES)
    # No nested value was unpacked into a child table either.
    assert {name for name in tables if not name.startswith("_dlt_")} == {
        VERCEL_PROJECTS_TABLE,
        VERCEL_DEPLOYMENTS_TABLE,
        VERCEL_BUILD_LOGS_TABLE,
    }
    # Guard the guard: the canaries really were in what the API returned.
    assert all(canary in json.dumps([fake.projects, fake.deployments]) for canary in CANARIES)


def test_unchanged_resync_produces_identical_rows(dlt_mod, tmp_path):
    # cognee hashes every column into the row id. Identical rows on a re-sync is
    # what keeps unchanged deployments from being re-cognified.
    fake = _fleet()
    first = _run_sync(dlt_mod, tmp_path, fake)
    before = {table: _read(first, table) for table in _TABLES}

    second = _run_sync(dlt_mod, tmp_path, fake)
    assert {table: _read(second, table) for table in _TABLES} == before


def test_state_change_after_creation_is_reflected_on_resync(dlt_mod, tmp_path):
    # A cursor on creation time would never look at this deployment again.
    fake = _fleet()
    fake.deployments.append(_deployment("dpl_new", "web", state="BUILDING", minutes_ago=1))
    pipeline = _run_sync(dlt_mod, tmp_path, fake)
    assert "State: BUILDING" in _read(pipeline, VERCEL_DEPLOYMENTS_TABLE)["dpl_new"]["content"]
    assert "dpl_new" not in _read(pipeline, VERCEL_BUILD_LOGS_TABLE)

    building = fake.deployments.pop()
    fake.deployments.append(
        {**building, "state": "ERROR", "readyState": "ERROR", "errorCode": "BUILD_FAILED"}
    )
    fake.logs["dpl_new"] = _log("exit code 1")
    pipeline = _run_sync(dlt_mod, tmp_path, fake)

    assert "State: ERROR" in _read(pipeline, VERCEL_DEPLOYMENTS_TABLE)["dpl_new"]["content"]
    assert _read(pipeline, VERCEL_BUILD_LOGS_TABLE)["dpl_new"]["content"] == "exit code 1"


def test_deleted_deployment_is_removed_on_resync(dlt_mod, tmp_path):
    fake = _fleet()
    _run_sync(dlt_mod, tmp_path, fake)

    # dpl_ok1 is gone from the listing; dpl_err is still listed but soft-deleted.
    fake.deployments = [
        item if item["uid"] != "dpl_err" else {**item, "state": "DELETED", "readyState": "DELETED"}
        for item in fake.deployments
        if item["uid"] != "dpl_ok1"
    ]
    pipeline = _run_sync(dlt_mod, tmp_path, fake)

    # Absent from staging is what cognee's orphan cleanup reconciles against.
    assert set(_read(pipeline, VERCEL_DEPLOYMENTS_TABLE)) == {"dpl_ok2"}
    # The failed build is gone, so its log goes too, even though that empties the table.
    assert _read(pipeline, VERCEL_BUILD_LOGS_TABLE) == {}


def test_deployment_older_than_the_window_leaves_the_snapshot(dlt_mod, tmp_path):
    fake = _fleet()
    fake.deployments.append(_deployment("dpl_old", "web", minutes_ago=3 * 24 * 60))

    everything = _run_sync(dlt_mod, tmp_path, fake, lookback_days=None)
    assert "dpl_old" in _read(everything, VERCEL_DEPLOYMENTS_TABLE)

    windowed = _run_sync(dlt_mod, tmp_path, fake, lookback_days=1)
    assert "dpl_old" not in _read(windowed, VERCEL_DEPLOYMENTS_TABLE)


def test_project_selection_limits_every_resource(dlt_mod, tmp_path):
    fake = _fleet()
    fake.deployments.append(_deployment("dpl_err_api", "api", state="ERROR", minutes_ago=5))
    fake.logs["dpl_err_api"] = _log("api build failed")

    pipeline = _run_sync(dlt_mod, tmp_path, fake, project_ids=["prj_api"])

    assert set(_read(pipeline, VERCEL_PROJECTS_TABLE)) == {"prj_api"}
    assert set(_read(pipeline, VERCEL_DEPLOYMENTS_TABLE)) == {"dpl_ok2", "dpl_err_api"}
    assert fake.event_paths() == ["/v3/deployments/dpl_err_api/events"]


def test_build_logs_can_be_switched_off(dlt_mod, tmp_path):
    fake = _fleet()
    pipeline = _run_sync(dlt_mod, tmp_path, fake, include_build_logs=False)

    assert fake.event_paths() == []
    assert set(_read(pipeline, VERCEL_DEPLOYMENTS_TABLE)) == {"dpl_ok1", "dpl_ok2", "dpl_err"}


def test_failed_deployment_without_a_log_still_has_its_document(dlt_mod, tmp_path):
    # A build that never started has no log; the events call answers 404.
    fake = _fleet()
    fake.logs = {}
    pipeline = _run_sync(dlt_mod, tmp_path, fake)

    deployments = _read(pipeline, VERCEL_DEPLOYMENTS_TABLE)
    assert "Error: BUILD_FAILED" in deployments["dpl_err"]["content"]
    assert fake.event_paths() == ["/v3/deployments/dpl_err/events"]


# ---------------------------------------------------------------------------
# Error handling: under replace, a partial snapshot must not forget live rows
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "failing_call",
    [
        pytest.param(lambda path, params: path == "/v10/projects", id="projects"),
        pytest.param(
            lambda path, params: path == "/v7/deployments" and "until" in params,
            id="deployments-page-2",
        ),
        pytest.param(lambda path, params: path.endswith("/events"), id="events"),
    ],
)
def test_http_error_aborts_sync_and_keeps_previous_snapshot(dlt_mod, tmp_path, failing_call):
    fake = _fleet()
    first = _run_sync(dlt_mod, tmp_path, fake)
    before = {table: _read(first, table) for table in _TABLES}

    fake.fail = lambda path, params: 500 if failing_call(path, params) else None
    with pytest.raises(Exception):  # noqa: B017 - dlt wraps the HTTP error in PipelineStepFailed
        _run_sync(dlt_mod, tmp_path, fake)

    # Nothing was loaded, so staging (and cognee's memory) still holds the last
    # complete snapshot instead of a short one.
    after = _pipeline(dlt_mod, tmp_path)
    assert {table: _read(after, table) for table in _TABLES} == before


def test_unexpected_list_shape_aborts_sync(dlt_mod, tmp_path):
    # The projects endpoint is documented with a bare-array body too. Selecting
    # "projects" from an array yields nothing and raises nothing, which under
    # replace would empty the table.
    fake = _fleet()
    first = _run_sync(dlt_mod, tmp_path, fake)
    before = _read(first, VERCEL_PROJECTS_TABLE)

    fake.projects_as_array = True
    with pytest.raises(Exception, match="expected an object with a 'projects' list"):
        _run_sync(dlt_mod, tmp_path, fake)

    assert _read(_pipeline(dlt_mod, tmp_path), VERCEL_PROJECTS_TABLE) == before


_TABLES = (VERCEL_PROJECTS_TABLE, VERCEL_DEPLOYMENTS_TABLE, VERCEL_BUILD_LOGS_TABLE)


# ---------------------------------------------------------------------------
# cognee: a deleted deployment leaves the graph, not only staging
# ---------------------------------------------------------------------------

# Distinctive project names, so each deployment maps to exactly one graph entity.
ALPHA = "alphacorp"
BRAVO = "bravocorp"


async def _mock_structured_output(text_input=None, system_prompt=None, response_model=str, **_):
    """Extract one entity named after whichever token appears in the chunk text."""
    from cognee.shared.data_models import KnowledgeGraph, SummarizedContent
    from cognee.shared.data_models import Node as KGNode

    if response_model is str:
        return "Mocked answer."
    if response_model == SummarizedContent:
        return SummarizedContent(summary="Mock summary", description="Mock summary")
    if response_model == KnowledgeGraph:
        name = next((token for token in (ALPHA, BRAVO) if text_input and token in text_input), None)
        nodes = [KGNode(id=name, name=name, type="Project", description=name)] if name else []
        return KnowledgeGraph(nodes=nodes, edges=[])
    return response_model()


async def _graph_has(token):
    from cognee.infrastructure.databases.graph import get_graph_engine

    nodes, _ = await (await get_graph_engine()).get_graph_data()
    return any(
        token in str(value).lower() for _, props in nodes for value in (props or {}).values()
    )


def test_deleted_deployment_is_forgotten_from_the_graph(tmp_path, monkeypatch):
    # Runs the source through cognee.add() + cognify() (LLM and embeddings mocked,
    # no credentials) to prove the document tag works on a rest_api source with
    # several tables and that orphan cleanup reaches the graph.
    pytest.importorskip("dlt")
    pytest.importorskip("ladybug")
    import asyncio
    import importlib

    import cognee
    from cognee.infrastructure.databases.vector.embeddings.LiteLLMEmbeddingEngine import (
        LiteLLMEmbeddingEngine,
    )
    from cognee.infrastructure.llm import LLMGateway

    add_data_points_module = importlib.import_module("cognee.tasks.storage.add_data_points")
    dataset = "vercel_forget_test"

    monkeypatch.setenv("COGNEE_SKIP_CONNECTION_TEST", "true")
    monkeypatch.setenv("ENABLE_BACKEND_ACCESS_CONTROL", "false")
    monkeypatch.setenv("DLT_DATA_DIR", str(tmp_path / "dlt"))
    monkeypatch.setenv("PIPELINES_DIR", str(tmp_path / "dlt" / "pipelines"))
    cognee.config.data_root_directory(str(tmp_path / "data"))
    cognee.config.system_root_directory(str(tmp_path / "system"))
    cognee.config.set_relational_db_config({"db_provider": "sqlite"})

    async def _noop_index(*_args, **_kwargs):
        return None

    async def _mock_embed_text(self, text):
        return [[0.0] * self.get_vector_size() for _ in text]

    monkeypatch.setattr(add_data_points_module, "index_data_points", _noop_index)
    monkeypatch.setattr(add_data_points_module, "index_graph_edges", _noop_index)
    monkeypatch.setattr(LLMGateway, "acreate_structured_output", _mock_structured_output)
    monkeypatch.setattr(LiteLLMEmbeddingEngine, "embed_text", _mock_embed_text)

    fake = FakeVercel(
        projects=[],
        deployments=[
            _deployment("dpl_alpha", ALPHA, minutes_ago=20),
            _deployment("dpl_bravo", BRAVO, state="ERROR", minutes_ago=10),
        ],
        logs={"dpl_bravo": _log(f"{BRAVO} build failed")},
    )

    async def sync():
        await cognee.add(
            vercel_source(token="test-token", session=fake.session()), dataset_name=dataset
        )
        await cognee.cognify(datasets=[dataset])

    async def scenario():
        await cognee.prune.prune_data()
        await cognee.prune.prune_system(metadata=True)
        try:
            await sync()
            assert await _graph_has(ALPHA)
            assert await _graph_has(BRAVO)

            # The failed deployment is deleted upstream: its document and its build
            # log document both drop out of the snapshot.
            fake.deployments = [item for item in fake.deployments if item["uid"] != "dpl_bravo"]
            await sync()
            assert await _graph_has(ALPHA), "the surviving deployment must stay"
            assert not await _graph_has(BRAVO), "the deleted deployment must leave the graph"
        finally:
            await cognee.prune.prune_data()
            await cognee.prune.prune_system(metadata=True)

    asyncio.run(scenario())
