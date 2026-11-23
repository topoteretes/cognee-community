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
import itertools
import json
import sqlite3
import threading
import time
from urllib.parse import parse_qs, urlsplit

import pytest
import requests
from dlt.pipeline.exceptions import PipelineStepFailed
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
    _default_session,
    _deployment_row,
    _event_text,
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


_UPDATED = itertools.count()


def _project(project_id, name):
    """A project shaped like the real list response, secrets included.

    Each one gets its own ``updatedAt``, which is what the list pages by.
    """
    return {
        "id": project_id,
        "name": name,
        "framework": "nextjs",
        "nodeVersion": "22.x",
        "rootDirectory": "apps/web",
        "createdAt": NOW_MS - 90 * DAY_MS,
        "updatedAt": NOW_MS - next(_UPDATED),
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

    def __init__(self, projects, deployments, logs=None, page_size=2, account="team_1"):
        super().__init__()
        # Like the real API, a project carries the accountId that owns it, and a
        # token answers 403 for a teamId it does not belong to.
        self.account = account
        self.omit_account = False  # serve projects without accountId
        self.before = None  # callable(path, params) run before answering, e.g. an edit
        self.timeouts = []  # the timeout each request was sent with
        self.projects = projects
        self.deployments = deployments
        self.logs = logs or {}
        self.page_size = page_size
        self.calls = []
        self.fail = None  # callable(path, params) -> status code to return, or None
        self.projects_as_array = False
        self.drop_pagination = set()  # paths whose list bodies lose their pagination block
        self.events_as_object = False  # events body that is not a list
        self.not_json = set()  # paths that answer 200 with an HTML page
        self.retry_after = "0"  # Retry-After sent with every failure

    def session(self):
        session = requests.Session()
        session.mount("https://", self)
        return session

    def send(self, request, **kwargs):
        url = urlsplit(request.url)
        multi = parse_qs(url.query)
        params = {key: values[0] for key, values in multi.items()}
        params["projectIds"] = multi.get("projectIds", [])
        self.calls.append((url.path, params, request.headers.get("Authorization")))
        self.timeouts.append(kwargs.get("timeout"))
        if self.before:
            self.before(url.path, params)
        if len(self.calls) > 500:
            # A cursor that never moves would loop forever; fail the test instead.
            raise AssertionError("FakeVercel: more than 500 requests in one test")

        if url.path in self.not_json:
            response = self._response(request, 200, None)
            response._content = b"<html>maintenance</html>"
            return response
        status = self.fail(url.path, params) if self.fail else None
        if isinstance(status, Exception):
            if isinstance(status, requests.RequestException) and status.request is None:
                status.request = request  # as requests' own adapter sets it
            raise status  # e.g. a dropped connection
        if status:
            response = self._response(request, status, {"error": {"code": "boom"}})
            response.headers["Retry-After"] = self.retry_after
            return response
        if params.get("teamId") not in (None, self.account):
            return self._response(request, 403, {"error": {"code": "forbidden"}})
        if url.path == "/v10/projects":
            owned = [
                project if self.omit_account else {"accountId": self.account, **project}
                for project in self.projects
            ]
            page = self._page(owned, "updatedAt", "projects", params)
            body = page["projects"] if self.projects_as_array else page
            if url.path in self.drop_pagination and isinstance(body, dict):
                body.pop("pagination")
            return self._response(request, 200, body)
        if url.path == "/v7/deployments":
            wanted_states = set(filter(None, params.get("state", "").split(",")))
            since = int(params.get("since", 0))
            # Like the real API: projectId takes an id or a name (any case),
            # resolves it to the project, and answers 404 for an unknown one;
            # projectIds takes at most 20 ids and returns nothing, with no error,
            # for a name.
            one, many = params.get("projectId"), params["projectIds"]
            if len(many) > 20:
                return self._response(request, 400, {"error": {"code": "bad_request"}})
            if one:
                found = [
                    item["id"]
                    for item in self.projects
                    if one == item["id"] or one.lower() == item["name"].lower()
                ]
                if not found:
                    return self._response(request, 404, {"error": {"code": "not_found"}})
                many = found
            items = [
                item
                for item in self.deployments
                if item["created"] >= since
                and (not wanted_states or item.get("state") in wanted_states)
                and (not many or item["projectId"] in many)
            ]
            page = self._page(items, "created", "deployments", params)
            if url.path in self.drop_pagination:
                page.pop("pagination")
            return self._response(request, 200, page)
        if url.path.startswith("/v3/deployments/") and url.path.endswith("/events"):
            uid = url.path.split("/")[3]
            if uid not in self.logs:
                return self._response(request, 404, {"error": {"code": "not_found"}})
            if self.events_as_object:
                return self._response(request, 200, {"events": self.logs[uid]})
            # Like the real API: backward returns the newest lines first, and a
            # positive limit keeps that many from the start of that order.
            events = list(self.logs[uid])
            if params.get("direction") == "backward":
                events.reverse()
            limit = int(params.get("limit", -1))
            return self._response(request, 200, events if limit < 0 else events[:limit])
        return self._response(request, 404, {"error": {"code": "not_found"}})

    def _page(self, items, order_key, list_key, params):
        # Like the real API: `until` is exclusive, and `next` is the timestamp of
        # the page's last item, so a second item from that millisecond is only
        # reached by asking for until=next+1.
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


def test_rows_drop_control_characters():
    # A NUL in a commit message would make Postgres refuse the whole load.
    deployment = _deployment("dpl_1", "web\x00site", state="ERROR")
    deployment["errorMessage"] = "boom\x00\x1b at line 3"
    deployment["meta"]["githubCommitMessage"] = "fix\x07 the\x00 build"
    project = _project("prj_web", "web\x00site")
    for row in (_deployment_row(deployment), _project_row(project)):
        for value in row.values():
            assert value is None or not any(ch in value for ch in "\x00\x07\x1b")
    assert "Commit message: fix the build" in _deployment_row(deployment)["content"]


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


def test_build_log_rows_put_newest_first_lines_back_in_order():
    newest_first = list(reversed(_events("dpl_err", "one", "two", "three")))
    (row,) = _build_log_rows(newest_first, max_log_chars=None, line_limit=5)
    assert row["content"] == "one\ntwo\nthree"


def test_build_log_rows_mark_output_cut_by_the_line_limit():
    # line_limit + 1 lines came back, so earlier output exists.
    newest_first = list(reversed(_events("dpl_err", "zero", "one", "two")))
    (row,) = _build_log_rows(newest_first, max_log_chars=None, line_limit=2)
    assert row["content"] == "[earlier output truncated]\none\ntwo"


def test_build_log_rows_keep_deployments_apart():
    events = _events("dpl_a", "log a") + _events("dpl_b", "log b")
    rows = {row["id"]: row["content"] for row in _build_log_rows(events, max_log_chars=None)}
    assert rows == {"dpl_a": "log a", "dpl_b": "log b"}


# ---------------------------------------------------------------------------
# Source construction
# ---------------------------------------------------------------------------


def test_a_team_gets_its_own_tables_and_state():
    from cognee.tasks.ingestion.dlt_utils import PIPELINE_SCOPE_ATTR

    source = vercel_source(token="test-token", team_id="team_AbC1", include_build_logs=True)
    assert set(source.resources.selected) == {
        "vercel_team_abc1_projects",
        "vercel_team_abc1_deployments",
        "vercel_team_abc1_build_logs",
    }
    assert getattr(source, PIPELINE_SCOPE_ATTR) == "vercel_team_abc1"
    named = vercel_source(token="test-token", team_id="team_AbC1", resource_name="vercel_main")
    assert "vercel_main_projects" in named.resources.selected


@pytest.mark.parametrize(
    "bad", ["Vercel", "vercel-prod", "1vercel", "vercel prod", "vercel__prod", "vercel_"]
)
def test_resource_name_must_fit_a_table_name(bad):
    with pytest.raises(ValueError, match="resource_name"):
        vercel_source(token="test-token", resource_name=bad)


def test_rows_do_not_depend_on_the_order_of_fields():
    # Vercel does not promise an order for object keys, meta entries or env lists.
    project = _project("prj_web", "web")
    shuffled = dict(reversed(list(project.items())))
    shuffled["link"] = dict(reversed(list(project["link"].items())))
    shuffled["env"] = list(reversed(project["env"]))
    assert _project_row(shuffled) == _project_row(project)

    deployment = _deployment("dpl_err", "web", state="ERROR")
    shuffled = dict(reversed(list(deployment.items())))
    shuffled["meta"] = dict(reversed(list(deployment["meta"].items())))
    assert _deployment_row(shuffled) == _deployment_row(deployment)


def test_vercel_source_keeps_its_state_per_dataset():
    # The empty-account guard lives in dlt state; without a scope every unscoped
    # dlt source in cognee would share one pipeline state.
    from cognee.tasks.ingestion.dlt_utils import PIPELINE_SCOPE_ATTR

    assert getattr(vercel_source(token="test-token"), PIPELINE_SCOPE_ATTR) == "vercel"


def test_event_text_drops_colour_codes_and_carriage_returns():
    assert _event_text({"text": "\x1b[31mError\x1b[0m: boom\r\n"}) == "Error: boom"
    # Progress output overwrites itself with \r: each step becomes its own line.
    assert _event_text({"text": "50%\r100%"}) == "50%\n100%"


def test_event_text_drops_control_characters_and_terminal_titles():
    # A NUL makes Postgres refuse the whole load; OSC sets a window title or link.
    text = "\x1b]0;my title\x07\x1b]8;;https://x\x1b\\link\x1b]8;;\x1b\\ ok\x00\x08\tdone"
    assert _event_text({"text": text}) == "link ok\tdone"
    # A lone surrogate cannot be encoded as UTF-8; it is replaced, not fatal.
    assert _event_text({"text": "bad \ud800 byte"}) == "bad ? byte"


def test_event_text_keeps_the_text_after_an_unclosed_escape():
    # An OSC that never ends must not swallow the error after it.
    assert "Error: real failure" in _event_text(
        {"text": "a\x1b]0;title never ends Error: real failure"}
    )
    # Charset and two-character escapes, as `tput sgr0` and full-screen tools write them.
    assert _event_text({"text": "\x1b(Bok \x1b=a\x1b>b \x1bMc \x1b7d\x1b8"}) == "ok ab c d"


def test_a_staging_read_that_fails_aborts_instead_of_dropping_the_log(
    dlt_mod, tmp_path, monkeypatch
):
    # Vercel no longer serves an expired log, so dropping the kept copy would
    # lose it for good. Only a missing table means there is nothing to keep.
    from dlt.destinations.exceptions import DatabaseTransientException

    from cognee_community_connector_vercel import vercel

    fake = _fleet()
    before = _read(_run_sync(dlt_mod, tmp_path, fake), VERCEL_BUILD_LOGS_TABLE)
    fake.fail = lambda path, params: 410 if path.endswith("/events") else None

    def locked(pipeline, table):
        raise DatabaseTransientException(Exception("database is locked"))

    monkeypatch.setattr(vercel, "_select_staged", locked)
    with pytest.raises(PipelineStepFailed, match="database is locked"):
        _run_sync(dlt_mod, tmp_path, fake)
    assert _read(_pipeline(dlt_mod, tmp_path), VERCEL_BUILD_LOGS_TABLE) == before


def test_build_events_without_the_parent_field_abort():
    # If rest_api renamed the copied parent fields, every log would be dropped
    # without a word.
    with pytest.raises(ValueError, match="without"):
        list(_build_log_rows(_log("line"), max_log_chars=None))


def test_vercel_source_declares_document_marker():
    # resolve_dlt_sources routes on this marker; the tag it carries is the source name.
    from cognee.tasks.ingestion.dlt_utils import document_source_tag

    source = vercel_source(token="test-token")
    assert VERCEL_SOURCE_NAME == "vercel"
    assert document_source_tag(source) == "vercel"


def test_vercel_source_requires_a_token_and_reads_no_environment(monkeypatch):
    # Environment variables are the example's business, not the factory's.
    monkeypatch.setenv("VERCEL_TOKEN", "from-the-environment")
    with pytest.raises(ValueError, match="token required"):
        vercel_source()


def test_vercel_source_selects_only_loadable_resources():
    # The raw lists, the failed-deployments parent and the per-line events are
    # plumbing: if they were loaded, cognee would read them back as documents.
    with_logs = vercel_source(token="test-token", include_build_logs=True)
    assert set(with_logs.resources) == {
        VERCEL_PROJECTS_TABLE,
        VERCEL_DEPLOYMENTS_TABLE,
        VERCEL_BUILD_LOGS_TABLE,
    }
    # Build logs are off unless asked for: a build can print a secret.
    assert set(vercel_source(token="test-token").resources) == {
        VERCEL_PROJECTS_TABLE,
        VERCEL_DEPLOYMENTS_TABLE,
    }


def test_vercel_source_takes_keyword_arguments_only():
    with pytest.raises(TypeError):
        vercel_source("test-token")


def test_a_team_id_that_cannot_name_tables_is_reported_as_the_team_id():
    with pytest.raises(ValueError, match="team_id 'team-AbC'"):
        vercel_source(token="test-token", team_id="team-AbC")


def test_project_ids_must_be_a_list():
    # set("prj_web") is its letters, which would match nothing.
    with pytest.raises(ValueError, match="project_ids must be a list"):
        vercel_source(token="test-token", project_ids="prj_web")


# ---------------------------------------------------------------------------
# dlt pipeline: snapshot sync, forget-on-delete, fail closed
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _no_telemetry(monkeypatch):
    # dlt 1.9 sends anonymous telemetry from a background thread that sleeps
    # between tries. Tests that record time.sleep must only see the connector's.
    monkeypatch.setenv("RUNTIME__DLTHUB_TELEMETRY", "false")


def _record_sleeps(monkeypatch):
    """Record time.sleep calls made on the main thread, which is where retries wait."""
    real_sleep = time.sleep
    slept = []

    def sleep(seconds):
        if threading.current_thread() is threading.main_thread():
            slept.append(seconds)
        else:
            real_sleep(seconds)

    monkeypatch.setattr(time, "sleep", sleep)
    return slept


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
    """Run vercel_source through a dlt pipeline into a temp sqlite destination.

    Build logs are off by default in the connector and on here, so the tests
    cover all three tables unless they say otherwise.
    """
    source_kwargs.setdefault("include_build_logs", True)
    source_kwargs.setdefault("service", fake.session())
    pipeline = _pipeline(dlt, tmp_path)
    pipeline.run(vercel_source(token="test-token", **source_kwargs))
    return pipeline


def _read(pipeline, table):
    """Return {id: {column: value}} for one staging table, dlt's own columns left out.

    cognee hashes every column that is not dlt's, ``url`` included, so a test that
    compared only title and content could miss a column that changes.
    """
    with (
        pipeline.sql_client() as client,
        client.execute_query(f"SELECT * FROM {table}") as cursor,
    ):
        names = list(cursor.keys())  # the sqlalchemy result behind dlt's cursor
        rows = cursor.fetchall()
    return {
        record["id"]: {key: value for key, value in record.items() if not key.startswith("_dlt")}
        for record in (dict(zip(names, row, strict=True)) for row in rows)
    }


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
    # Default cap of 20000 characters: only the newest 2000 lines, plus one to tell
    # whether earlier output exists.
    assert events_call[1]["direction"] == "backward"
    assert events_call[1]["limit"] == "2001"


def test_uncapped_logs_are_requested_in_full(dlt_mod, tmp_path):
    fake = _fleet()
    _run_sync(dlt_mod, tmp_path, fake, max_log_chars=None)
    (events_call,) = [call for call in fake.calls if call[0].endswith("/events")]
    assert events_call[1]["limit"] == "-1"
    assert "direction" not in events_call[1]


def test_only_the_end_of_a_long_log_is_requested(dlt_mod, tmp_path):
    fake = _fleet()
    fake.logs["dpl_err"] = _log(*(f"line {number:03d}" for number in range(300)))
    pipeline = _run_sync(dlt_mod, tmp_path, fake, max_log_chars=1000)

    (events_call,) = [call for call in fake.calls if call[0].endswith("/events")]
    assert events_call[1]["limit"] == "101"  # max(100, 1000 // 10) lines, plus one
    content = _read(pipeline, VERCEL_BUILD_LOGS_TABLE)["dpl_err"]["content"]
    lines = content.splitlines()
    # Back in reading order, cut at the front, and still ending on the last line.
    assert lines[0] == "[earlier output truncated]"
    assert lines[-1] == "line 299"
    assert lines[1:] == sorted(lines[1:])
    assert len(content) <= 1000 + len("[earlier output truncated]\n")


@pytest.mark.parametrize("cap", [0, -5])
def test_log_cap_must_be_positive(cap):
    with pytest.raises(ValueError, match="max_log_chars"):
        vercel_source(token="test-token", max_log_chars=cap)


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
    # The second request carries the first page's pagination.next plus one as
    # `until`, which is exclusive, so the page's last item is read again.
    oldest_on_page_one = sorted(item["created"] for item in fake.deployments)[1]
    assert deployment_pages[1]["until"] == str(oldest_on_page_one + 1)
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


def _deployment_calls(fake):
    return [params for path, params, _ in fake.calls if path == "/v7/deployments"]


def _fleet_with_docs():
    """The usual fleet plus a third project, never selected, with its own failed build."""
    fake = _fleet()
    fake.projects.append(_project("prj_docs", "docs"))
    fake.deployments.append(_deployment("dpl_docs_err", "docs", state="ERROR", minutes_ago=5))
    fake.logs["dpl_docs_err"] = _log("docs build failed")
    return fake


def _assert_docs_left_out(fake, pipeline):
    assert "prj_docs" not in _read(pipeline, VERCEL_PROJECTS_TABLE)
    assert "dpl_docs_err" not in _read(pipeline, VERCEL_DEPLOYMENTS_TABLE)
    assert "/v3/deployments/dpl_docs_err/events" not in fake.event_paths()


def test_one_selected_project_is_filtered_on_the_server(dlt_mod, tmp_path):
    fake = _fleet()
    pipeline = _run_sync(dlt_mod, tmp_path, fake, project_ids=["web"])  # a name works

    # The name is resolved through the projects list and sent as the id.
    assert {params.get("projectId") for params in _deployment_calls(fake)} == {"prj_web"}
    assert set(_read(pipeline, VERCEL_DEPLOYMENTS_TABLE)) == {"dpl_ok1", "dpl_err"}


def test_several_project_ids_are_filtered_on_the_server(dlt_mod, tmp_path):
    fake = _fleet_with_docs()
    pipeline = _run_sync(dlt_mod, tmp_path, fake, project_ids=["prj_web", "prj_api"])
    _assert_docs_left_out(fake, pipeline)

    assert all(params["projectIds"] == ["prj_api", "prj_web"] for params in _deployment_calls(fake))
    assert set(_read(pipeline, VERCEL_DEPLOYMENTS_TABLE)) == {"dpl_ok1", "dpl_ok2", "dpl_err"}


def test_a_name_in_a_list_is_sent_as_its_project_id(dlt_mod, tmp_path):
    # projectIds with a name returns no deployments and no error, which under
    # replace would forget them all. Names are resolved to ids first.
    fake = _fleet_with_docs()
    pipeline = _run_sync(dlt_mod, tmp_path, fake, project_ids=["web", "prj_api"])
    _assert_docs_left_out(fake, pipeline)

    assert all(params["projectIds"] == ["prj_api", "prj_web"] for params in _deployment_calls(fake))
    assert set(_read(pipeline, VERCEL_DEPLOYMENTS_TABLE)) == {"dpl_ok1", "dpl_ok2", "dpl_err"}


def test_a_project_named_like_an_id_is_still_found(dlt_mod, tmp_path):
    # Vercel allows "_" in names, so "prj_tools" can be a name. Sent as an id it
    # would match nothing and its deployments would be forgotten.
    fake = _fleet()
    fake.projects.append(_project("prj_9f3k", "prj_tools"))
    tools = _deployment("dpl_tools", "prj_tools", minutes_ago=5)
    tools["projectId"] = "prj_9f3k"
    fake.deployments.append(tools)
    pipeline = _run_sync(dlt_mod, tmp_path, fake, project_ids=["prj_web", "prj_tools"])

    assert set(_read(pipeline, VERCEL_PROJECTS_TABLE)) == {"prj_web", "prj_9f3k"}
    assert set(_read(pipeline, VERCEL_DEPLOYMENTS_TABLE)) == {"dpl_ok1", "dpl_err", "dpl_tools"}
    sent = {tuple(params["projectIds"]) for params in _deployment_calls(fake)}
    assert sent == {("prj_9f3k", "prj_web")}


def test_a_reused_source_moves_its_window(dlt_mod, tmp_path, monkeypatch):
    # The window is worked out per run. If it were fixed when the source was
    # built, a long-lived source would never let old deployments age out. The
    # connector's clock is replaced, so a wall-clock jump can't flip the result.
    from datetime import datetime as real_datetime
    from datetime import timedelta, timezone

    from cognee_community_connector_vercel import vercel

    start = real_datetime(2026, 10, 1, tzinfo=timezone.utc)
    ticks = iter([start, start + timedelta(hours=1)])

    class Clock(real_datetime):
        @classmethod
        def now(cls, tz=None):
            return next(ticks)

    monkeypatch.setattr(vercel, "datetime", Clock)
    fake = _fleet()
    source = vercel_source(token="test-token", service=fake.session())
    pipeline = _pipeline(dlt_mod, tmp_path)
    pipeline.run(source)
    first = {params["since"] for params in _deployment_calls(fake)}
    fake.calls.clear()
    pipeline.run(source)
    second = {params["since"] for params in _deployment_calls(fake)}
    assert len(first) == len(second) == 1
    assert int(second.pop()) - int(first.pop()) == 3_600_000, "one hour later"


def test_more_than_20_ids_are_filtered_after_listing(dlt_mod, tmp_path):
    # Vercel answers 400 above 20 projectIds, which would fail every run.
    fake = _fleet_with_docs()
    extra = [f"prj_extra{number}" for number in range(19)]
    fake.projects += [_project(item, item.removeprefix("prj_")) for item in extra]
    pipeline = _run_sync(dlt_mod, tmp_path, fake, project_ids=["prj_web", "prj_api", *extra])

    assert all(not params["projectIds"] for params in _deployment_calls(fake))
    assert set(_read(pipeline, VERCEL_DEPLOYMENTS_TABLE)) == {"dpl_ok1", "dpl_ok2", "dpl_err"}
    _assert_docs_left_out(fake, pipeline)


def test_a_project_name_matches_in_any_case(dlt_mod, tmp_path):
    # Vercel resolves `projectId=WEB` to the project "web". If the rows it returns
    # were then dropped here, every one of them would be forgotten.
    fake = _fleet()
    pipeline = _run_sync(dlt_mod, tmp_path, fake, project_ids=["WEB"])
    assert set(_read(pipeline, VERCEL_PROJECTS_TABLE)) == {"prj_web"}
    assert set(_read(pipeline, VERCEL_DEPLOYMENTS_TABLE)) == {"dpl_ok1", "dpl_err"}


def test_a_selected_project_that_is_gone_aborts_and_names_it(dlt_mod, tmp_path):
    # A deleted project, a renamed one or a typo look the same from here. Under
    # replace each would forget its documents, so the run stops and says which.
    fake = _fleet()
    first = _run_sync(dlt_mod, tmp_path, fake, project_ids=["web", "prj_api"])
    before = {table: _read(first, table) for table in _TABLES}

    fake.projects = [item for item in fake.projects if item["name"] != "web"]
    fake.deployments = [item for item in fake.deployments if item["name"] != "web"]
    with pytest.raises(
        PipelineStepFailed, match=r"not found among the 1 projects this token can see: \[.web.\]"
    ):
        _run_sync(dlt_mod, tmp_path, fake, project_ids=["web", "prj_api"])

    assert {table: _read(_pipeline(dlt_mod, tmp_path), table) for table in _TABLES} == before


def test_allow_empty_forgets_a_selected_project_that_is_gone(dlt_mod, tmp_path, monkeypatch):
    # For a one-entry selection, dropping the entry would sync the whole account.
    fake = _fleet()
    _run_sync(dlt_mod, tmp_path, fake, project_ids=["web"])
    _run_sync(dlt_mod, tmp_path, fake, project_ids=["web", "prj_api"])
    web = [item for item in fake.projects if item["name"] == "web"]
    fake.projects = [item for item in fake.projects if item["name"] != "web"]

    with pytest.raises(PipelineStepFailed, match=r"not found among the 1 projects.*\['web'\]"):
        _run_sync(dlt_mod, tmp_path, fake, project_ids=["web", "prj_api"])
    pipeline = _run_sync(dlt_mod, tmp_path, fake, project_ids=["web", "prj_api"], allow_empty=True)
    assert set(_read(pipeline, VERCEL_PROJECTS_TABLE)) == {"prj_api"}
    assert set(_read(pipeline, VERCEL_DEPLOYMENTS_TABLE)) == {"dpl_ok2"}

    # Later runs without the flag skip the forgotten entry instead of stopping,
    # and say so: in the counts, and in a warning naming the run that accepted it.
    from cognee_community_connector_vercel import vercel

    warnings = []
    monkeypatch.setattr(vercel.logger, "warning", lambda *args: warnings.append(args))
    source = vercel_source(
        token="test-token", service=fake.session(), project_ids=["web", "prj_api"]
    )
    pipeline = _pipeline(dlt_mod, tmp_path)
    pipeline.run(source)
    assert set(_read(pipeline, VERCEL_DEPLOYMENTS_TABLE)) == {"dpl_ok2"}
    assert source.cognee_sync_stats["skipped_projects"] == ["web"]
    (skipped,) = [args for args in warnings if "skipping project_ids" in args[0]]
    assert skipped[1] == "web" and skipped[2].endswith(" UTC")
    # A new missing entry still stops the run.
    with pytest.raises(PipelineStepFailed, match="docs"):
        _run_sync(dlt_mod, tmp_path, fake, project_ids=["web", "prj_api", "docs"])
    # And once the project is back, it is synced again.
    fake.projects += web
    pipeline = _run_sync(dlt_mod, tmp_path, fake, project_ids=["web", "prj_api"])
    assert set(_read(pipeline, VERCEL_DEPLOYMENTS_TABLE)) == {"dpl_ok1", "dpl_ok2", "dpl_err"}


def test_a_selection_that_is_all_gone_lists_no_deployments(dlt_mod, tmp_path):
    fake = _fleet()
    _run_sync(dlt_mod, tmp_path, fake, project_ids=["web"])
    fake.projects = [item for item in fake.projects if item["name"] != "web"]
    fake.calls.clear()
    pipeline = _run_sync(dlt_mod, tmp_path, fake, project_ids=["web"], allow_empty=True)
    assert _read(pipeline, VERCEL_DEPLOYMENTS_TABLE) == {}
    assert _deployment_calls(fake) == [], "nothing selected is left, so nothing is listed"
    assert fake.event_paths() == []


def test_the_missing_project_error_counts_every_project_the_token_sees(dlt_mod, tmp_path):
    fake = _fleet_with_docs()  # web, api and docs
    with pytest.raises(PipelineStepFailed, match=r"among the 3 projects this token can see"):
        _run_sync(dlt_mod, tmp_path, fake, project_ids=["web", "prj_typo"])


def test_loading_only_some_tables_is_refused_with_a_clear_message(dlt_mod, tmp_path):
    # The deployment tables use the projects list read in the same run.
    fake = _fleet()
    source = vercel_source(token="test-token", service=fake.session())
    with pytest.raises(PipelineStepFailed, match=r"with_resources\(\).*not supported"):
        _pipeline(dlt_mod, tmp_path).run(source.with_resources(VERCEL_DEPLOYMENTS_TABLE))
    with pytest.raises(NotImplementedError, match=r"parallelize\(\) is not supported"):
        source.parallelize()


def _guard_missing_harness(monkeypatch, state):
    from cognee_community_connector_vercel import vercel

    warnings = []
    monkeypatch.setattr(vercel.dlt.current, "source_state", lambda: state)
    monkeypatch.setattr(vercel.logger, "warning", lambda *args: warnings.append(args))
    return vercel, warnings


def test_a_skipped_project_keeps_the_time_of_the_run_that_accepted_it(monkeypatch):
    state = {}
    vercel, warnings = _guard_missing_harness(monkeypatch, state)
    api, web = _project("prj_api", "api"), _project("prj_web", "web")
    wanted = {"web", "prj_api"}

    # The accepting run already lists it and warns, with its own time.
    monkeypatch.setattr(vercel, "_now", lambda: "2026-10-01 09:00 UTC")
    stats = {}
    vercel._guard_missing(wanted, [api], 1, True, stats)
    assert stats["skipped_projects"] == ["web"]
    assert warnings[-1][1:] == ("web", "2026-10-01 09:00 UTC")

    # A later run without the flag keeps that time, not its own.
    monkeypatch.setattr(vercel, "_now", lambda: "2026-10-27 15:00 UTC")
    stats = {}
    vercel._guard_missing(wanted, [api], 1, False, stats)
    assert stats["skipped_projects"] == ["web"]
    assert warnings[-1][1:] == ("web", "2026-10-01 09:00 UTC")
    assert state["forgotten_projects"] == {"web": "2026-10-01 09:00 UTC"}

    # Found again, it drops out; missing again, it stops the run again.
    vercel._guard_missing(wanted, [api, web], 2, False, {})
    assert state["forgotten_projects"] == {}
    with pytest.raises(ValueError, match="not found"):
        vercel._guard_missing(wanted, [api], 1, False, {})


def test_skipped_projects_saved_by_an_earlier_commit_are_read(monkeypatch):
    # Earlier commits kept a plain list, without the time.
    state = {"forgotten_projects": ["web"]}
    vercel, warnings = _guard_missing_harness(monkeypatch, state)
    vercel._guard_missing({"web", "prj_api"}, [_project("prj_api", "api")], 1, False, {})
    assert warnings[-1][1:] == ("web", "time unknown")
    assert state["forgotten_projects"] == {"web": "time unknown"}


def test_the_projects_hand_over_refuses_a_missing_or_reused_list():
    from cognee_community_connector_vercel.vercel import _RunProjects

    run = _RunProjects()
    with pytest.raises(RuntimeError, match="needs this run's projects list"):
        run.take("deployments")
    run.publish([{"id": "prj_web"}])
    assert run.take("deployments") == [{"id": "prj_web"}]
    with pytest.raises(RuntimeError, match="needs this run's projects list"):
        run.take("deployments")  # a second run would get last run's list
    run.publish([])
    assert run.take("deployments") == []


def test_project_ids_that_match_nothing_abort(dlt_mod, tmp_path):
    fake = _fleet()
    with pytest.raises(PipelineStepFailed, match="prj_typo"):
        _run_sync(dlt_mod, tmp_path, fake, project_ids=["prj_typo", "prj_web"])


def test_a_renamed_project_keeps_its_older_deployments(dlt_mod, tmp_path):
    # Vercel picks deployments by project, but each one keeps the name it was
    # built under. Matching the new name against that would drop them all.
    fake = _fleet()
    fake.projects[0]["name"] = "site"  # was "web"; dpl_ok1 and dpl_err still say web
    pipeline = _run_sync(dlt_mod, tmp_path, fake, project_ids=["site"])

    assert set(_read(pipeline, VERCEL_PROJECTS_TABLE)) == {"prj_web"}
    assert set(_read(pipeline, VERCEL_DEPLOYMENTS_TABLE)) == {"dpl_ok1", "dpl_err"}
    assert fake.event_paths() == ["/v3/deployments/dpl_err/events"]


def test_a_renamed_project_among_several_names_keeps_its_deployments(dlt_mod, tmp_path):
    # Two names are filtered here, not by Vercel. Deployments are matched on the
    # project id the names resolve to, never on the name they were built under.
    fake = _fleet_with_docs()
    fake.projects[0]["name"] = "site"  # was "web"; dpl_ok1 and dpl_err still say web
    pipeline = _run_sync(dlt_mod, tmp_path, fake, project_ids=["site", "api"])

    assert set(_read(pipeline, VERCEL_DEPLOYMENTS_TABLE)) == {"dpl_ok1", "dpl_ok2", "dpl_err"}
    assert sorted(fake.event_paths()) == ["/v3/deployments/dpl_err/events"]
    _assert_docs_left_out(fake, pipeline)


@pytest.mark.parametrize("days", [0, -1])
def test_lookback_days_must_be_positive(days):
    with pytest.raises(ValueError, match="lookback_days"):
        vercel_source(token="test-token", lookback_days=days)


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
    with pytest.raises(PipelineStepFailed):
        _run_sync(dlt_mod, tmp_path, fake)

    # Nothing was loaded, so staging (and cognee's memory) still holds the last
    # complete snapshot instead of a short one.
    after = _pipeline(dlt_mod, tmp_path)
    assert {table: _read(after, table) for table in _TABLES} == before


def test_check_active_is_called_for_every_page(dlt_mod, tmp_path):
    fake = _fleet()
    calls = []
    _run_sync(dlt_mod, tmp_path, fake, check_active=lambda: calls.append(1))
    assert len(calls) == len(fake.calls)


def test_check_active_can_stop_a_sync(dlt_mod, tmp_path):
    # The host revokes access mid-run: the run aborts and memory stays as it was.
    fake = _fleet()
    first = _run_sync(dlt_mod, tmp_path, fake)
    before = {table: _read(first, table) for table in _TABLES}

    calls = []

    def revoked_after_two_pages():
        calls.append(1)
        if len(calls) > 2:
            raise PermissionError("access revoked")

    fake.deployments = fake.deployments[:1]
    with pytest.raises(PipelineStepFailed, match="access revoked"):
        _run_sync(dlt_mod, tmp_path, fake, check_active=revoked_after_two_pages)

    pipeline = _pipeline(dlt_mod, tmp_path)
    assert {table: _read(pipeline, table) for table in _TABLES} == before


def test_list_without_pagination_aborts_sync(dlt_mod, tmp_path):
    # dlt's cursor paginator stops quietly when the cursor is missing, which would
    # keep only the first page and forget the rest.
    fake = _fleet()
    first = _run_sync(dlt_mod, tmp_path, fake)
    before = {table: _read(first, table) for table in _TABLES}

    fake.drop_pagination = {"/v10/projects", "/v7/deployments"}
    with pytest.raises(PipelineStepFailed, match=r"pagination\.next"):
        _run_sync(dlt_mod, tmp_path, fake)

    pipeline = _pipeline(dlt_mod, tmp_path)
    assert {table: _read(pipeline, table) for table in _TABLES} == before


def test_deployment_list_without_pagination_aborts_sync(dlt_mod, tmp_path):
    # The projects list is fine here, so only the deployments check can stop it.
    fake = _fleet()
    first = _run_sync(dlt_mod, tmp_path, fake)
    before = _read(first, VERCEL_DEPLOYMENTS_TABLE)

    fake.drop_pagination = {"/v7/deployments"}
    with pytest.raises(PipelineStepFailed, match=r"pagination\.next") as raised:
        _run_sync(dlt_mod, tmp_path, fake, project_ids=["web"])
    # Status and endpoint only: the query (team id, project names) stays out of logs.
    assert "/v7/deployments" in str(raised.value)
    assert "projectId=" not in str(raised.value)

    assert _read(_pipeline(dlt_mod, tmp_path), VERCEL_DEPLOYMENTS_TABLE) == before


def test_events_body_that_is_not_a_list_aborts_sync(dlt_mod, tmp_path):
    fake = _fleet()
    first = _run_sync(dlt_mod, tmp_path, fake)
    before = _read(first, VERCEL_BUILD_LOGS_TABLE)

    fake.events_as_object = True
    with pytest.raises(PipelineStepFailed, match="list of build events") as raised:
        _run_sync(dlt_mod, tmp_path, fake)
    # The message names the endpoint, not the deployment.
    assert "/v3/deployments/{id}/events" in str(raised.value)
    assert "dpl_err" not in str(raised.value)

    assert _read(_pipeline(dlt_mod, tmp_path), VERCEL_BUILD_LOGS_TABLE) == before


def test_a_body_that_is_not_json_aborts(dlt_mod, tmp_path):
    fake = _fleet()
    first = _run_sync(dlt_mod, tmp_path, fake)
    before = {table: _read(first, table) for table in _TABLES}

    fake.not_json = {"/v7/deployments"}
    with pytest.raises(PipelineStepFailed, match="is not JSON"):
        _run_sync(dlt_mod, tmp_path, fake)
    assert {table: _read(_pipeline(dlt_mod, tmp_path), table) for table in _TABLES} == before


def test_one_source_object_run_twice_reads_everything_both_times(dlt_mod, tmp_path):
    # The paginator lives as long as the source. If its cursor carried over, the
    # second run would start mid-list and forget the newest deployments.
    fake = _fleet()
    source = vercel_source(token="test-token", service=fake.session(), include_build_logs=True)
    pipeline = _pipeline(dlt_mod, tmp_path)
    pipeline.run(source)
    first = {table: _read(pipeline, table) for table in _TABLES}
    fake.calls.clear()
    pipeline.run(source)
    assert {table: _read(pipeline, table) for table in _TABLES} == first
    assert "until" not in _deployment_calls(fake)[0], "the second run starts at the newest page"


def test_an_older_cognee_is_refused(monkeypatch):
    # Without table-scoped cleanup, a sync of one table would forget the other two.
    from cognee.tasks.ingestion import dlt_utils

    monkeypatch.setattr(dlt_utils, "DOCUMENT_SYNC_VERSION", 0)
    with pytest.raises(RuntimeError, match=r"cognee>=1\.6\.1"):
        vercel_source(token="test-token")


def test_a_cognee_without_the_scope_attribute_is_refused(monkeypatch):
    # A dev build can carry the version number but not PIPELINE_SCOPE_ATTR yet.
    from cognee.tasks.ingestion import dlt_utils

    monkeypatch.delattr(dlt_utils, "PIPELINE_SCOPE_ATTR")
    with pytest.raises(RuntimeError, match=r"cognee>=1\.6\.1"):
        vercel_source(token="test-token")


def test_an_account_that_suddenly_lists_no_projects_aborts(dlt_mod, tmp_path):
    # A token for the wrong team answers with a valid, empty list. Under replace
    # that would forget every project, deployment and log.
    fake = _fleet()
    first = _run_sync(dlt_mod, tmp_path, fake)
    before = {table: _read(first, table) for table in _TABLES}

    fake.projects, fake.deployments = [], []
    with pytest.raises(PipelineStepFailed, match="no projects at all"):
        _run_sync(dlt_mod, tmp_path, fake)

    pipeline = _pipeline(dlt_mod, tmp_path)
    assert {table: _read(pipeline, table) for table in _TABLES} == before


def test_a_token_for_another_account_aborts(dlt_mod, tmp_path, monkeypatch):
    # Another account still has projects, so the empty check alone would let it
    # through, and replace would forget every document of the first account.
    fake = _fleet()
    first = _run_sync(dlt_mod, tmp_path, fake)
    before = {table: _read(first, table) for table in _TABLES}

    other = FakeVercel(
        account="team_2",
        projects=[_project("prj_x", "x")],
        deployments=[_deployment("dpl_x", "x")],
    )
    with pytest.raises(PipelineStepFailed, match="another account"):
        _run_sync(dlt_mod, tmp_path, other)
    assert {table: _read(_pipeline(dlt_mod, tmp_path), table) for table in _TABLES} == before

    # Meant on purpose: allow_empty lets the switch through once, with a warning.
    from cognee_community_connector_vercel import vercel

    warnings = []
    monkeypatch.setattr(vercel.logger, "warning", lambda *args: warnings.append(args))
    pipeline = _run_sync(dlt_mod, tmp_path, other, allow_empty=True)
    assert set(_read(pipeline, VERCEL_PROJECTS_TABLE)) == {"prj_x"}
    assert any("another account" in args[0] for args in warnings)
    _run_sync(dlt_mod, tmp_path, other)


def test_projects_without_an_account_id_keep_the_account_check(dlt_mod, tmp_path):
    # A list that carries no accountId must not clear what the check compares to.
    fake = _fleet()
    _run_sync(dlt_mod, tmp_path, fake)
    fake.omit_account = True
    _run_sync(dlt_mod, tmp_path, fake)

    other = FakeVercel(account="team_2", projects=[_project("prj_x", "x")], deployments=[])
    with pytest.raises(PipelineStepFailed, match="another account"):
        _run_sync(dlt_mod, tmp_path, other)


def test_a_project_edited_while_the_list_pages_is_kept(dlt_mod, tmp_path):
    # Projects page by updatedAt, newest first. One edited during the paging
    # moves to the front, behind the cursor; the newest page is read again at
    # the end, so it is not dropped, and neither are its deployments.
    fake = _fleet_with_docs()  # three projects on pages of two
    oldest = min(fake.projects, key=lambda project: project["updatedAt"])

    def edit_during_page_two(path, params):
        if path == "/v10/projects" and "until" in params:
            oldest["updatedAt"] = NOW_MS + 1000
            oldest["framework"] = "astro"

    fake.before = edit_during_page_two
    pipeline = _run_sync(dlt_mod, tmp_path, fake, project_ids=["web", "api", "docs"])

    projects = _read(pipeline, VERCEL_PROJECTS_TABLE)
    assert set(projects) == {"prj_web", "prj_api", "prj_docs"}
    assert "Framework: astro" in projects[oldest["id"]]["content"], "the newer copy wins"
    assert set(_read(pipeline, VERCEL_DEPLOYMENTS_TABLE)) == {
        "dpl_ok1",
        "dpl_ok2",
        "dpl_err",
        "dpl_docs_err",
    }
    # The projects are read once per run: two pages and the newest page again.
    assert len([call for call in fake.calls if call[0] == "/v10/projects"]) == 3


def test_a_project_added_or_removed_in_the_same_account_is_fine(dlt_mod, tmp_path):
    fake = _fleet()
    _run_sync(dlt_mod, tmp_path, fake)
    fake.projects = [_project("prj_new", "new")]  # same accountId, all-new projects
    pipeline = _run_sync(dlt_mod, tmp_path, fake)
    assert set(_read(pipeline, VERCEL_PROJECTS_TABLE)) == {"prj_new"}


def test_allow_empty_lets_an_emptied_account_through(dlt_mod, tmp_path):
    fake = _fleet()
    _run_sync(dlt_mod, tmp_path, fake)

    fake.projects, fake.deployments = [], []
    pipeline = _run_sync(dlt_mod, tmp_path, fake, allow_empty=True)
    assert _read(pipeline, VERCEL_PROJECTS_TABLE) == {}
    assert _read(pipeline, VERCEL_DEPLOYMENTS_TABLE) == {}
    # Once the empty account is accepted, later empty syncs are fine as well.
    _run_sync(dlt_mod, tmp_path, fake)


def test_first_sync_of_an_empty_account_is_fine(dlt_mod, tmp_path):
    fake = FakeVercel(projects=[], deployments=[])
    _run_sync(dlt_mod, tmp_path, fake)  # nothing was synced before, so nothing to protect


def test_unexpected_list_shape_aborts_sync(dlt_mod, tmp_path):
    # The projects endpoint is documented with a bare-array body too. Selecting
    # "projects" from an array yields nothing and raises nothing, which under
    # replace would empty the table.
    fake = _fleet()
    first = _run_sync(dlt_mod, tmp_path, fake)
    before = _read(first, VERCEL_PROJECTS_TABLE)

    fake.projects_as_array = True
    with pytest.raises(PipelineStepFailed, match="expected an object with a 'projects' list"):
        _run_sync(dlt_mod, tmp_path, fake)

    assert _read(_pipeline(dlt_mod, tmp_path), VERCEL_PROJECTS_TABLE) == before


def test_sync_stats_count_the_rows_of_the_last_run(dlt_mod, tmp_path):
    fake = _fleet()
    source = vercel_source(token="test-token", service=fake.session(), include_build_logs=True)
    _pipeline(dlt_mod, tmp_path).run(source)
    assert source.cognee_sync_stats == {
        "projects": 2,
        "deployments": 3,
        "build_logs": 1,
        "failed_rate_limit": 0,
        "failed_auth": 0,
        "skipped_projects": [],
    }


def test_without_a_service_the_capped_retrying_session_is_used(dlt_mod, tmp_path, monkeypatch):
    from cognee_community_connector_vercel import vercel

    fake = _fleet()
    made = []

    def capped():
        session = _default_session()
        session.mount("https://", fake)
        made.append(session)
        return session

    monkeypatch.setattr(vercel, "_default_session", capped)
    pipeline = _pipeline(dlt_mod, tmp_path)
    pipeline.run(vercel_source(token="test-token"))
    assert len(made) == 1
    assert set(_read(pipeline, VERCEL_PROJECTS_TABLE)) == {"prj_web", "prj_api"}


def test_a_table_that_empties_logs_a_warning(dlt_mod, tmp_path, monkeypatch):
    from cognee_community_connector_vercel import vercel

    warnings = []
    monkeypatch.setattr(vercel.logger, "warning", lambda *args: warnings.append(args))
    fake = _fleet()
    _run_sync(dlt_mod, tmp_path, fake)
    assert warnings == []
    fake.logs = {}
    fake.deployments = [item for item in fake.deployments if item["state"] != "ERROR"]
    _run_sync(dlt_mod, tmp_path, fake)
    assert [args[1:] for args in warnings] == [(VERCEL_BUILD_LOGS_TABLE, 1)]


def test_a_deployment_with_only_ready_state_deleted_is_left_out(dlt_mod, tmp_path):
    # Vercel's spec makes readyState the required field and state optional.
    fake = _fleet()
    gone = _deployment("dpl_gone", "web", minutes_ago=5, readyState="DELETED")
    del gone["state"]
    fake.deployments.append(gone)
    pipeline = _run_sync(dlt_mod, tmp_path, fake)
    assert set(_read(pipeline, VERCEL_DEPLOYMENTS_TABLE)) == {"dpl_ok1", "dpl_ok2", "dpl_err"}


def test_two_deployments_from_one_millisecond_at_a_page_boundary_are_both_kept(dlt_mod, tmp_path):
    # `until` is exclusive and `next` is the last item's timestamp, so without the
    # overlap the twin after the boundary would never be read, and be forgotten.
    fake = _fleet()
    fake.page_size = 3  # page one ends on dpl_a, page two starts with its twin
    first, twin = (
        _deployment(uid, "web", state="ERROR", minutes_ago=15) for uid in ("dpl_a", "dpl_b")
    )
    twin["created"] = first["created"]
    # dpl_new failed too, so the failed-only list has its page boundary between
    # the twins as well.
    fake.deployments += [first, twin, _deployment("dpl_new", "api", state="ERROR", minutes_ago=12)]
    fake.logs.update(dpl_a=_log("a failed"), dpl_b=_log("b failed"), dpl_new=_log("new failed"))
    pipeline = _run_sync(dlt_mod, tmp_path, fake)

    deployments = _read(pipeline, VERCEL_DEPLOYMENTS_TABLE)
    assert set(deployments) == {"dpl_ok1", "dpl_ok2", "dpl_err", "dpl_new", "dpl_a", "dpl_b"}
    with pipeline.sql_client() as client:
        (rows,) = client.execute_sql(f"SELECT COUNT(*) FROM {VERCEL_DEPLOYMENTS_TABLE}")[0]
    assert rows == 6, "an item read twice is loaded once"
    # The repeated item costs no second events request either.
    assert sorted(fake.event_paths()) == sorted(set(fake.event_paths()))
    assert len(fake.event_paths()) == 4
    assert set(_read(pipeline, VERCEL_BUILD_LOGS_TABLE)) == {"dpl_err", "dpl_new", "dpl_a", "dpl_b"}
    with pipeline.sql_client() as client:
        (logs,) = client.execute_sql(f"SELECT COUNT(*) FROM {VERCEL_BUILD_LOGS_TABLE}")[0]
    assert logs == 4


def test_a_project_read_twice_at_a_page_boundary_is_loaded_once(dlt_mod, tmp_path):
    fake = _fleet_with_docs()  # three projects on pages of two
    pipeline = _run_sync(dlt_mod, tmp_path, fake)
    with pipeline.sql_client() as client:
        (rows,) = client.execute_sql(f"SELECT COUNT(*) FROM {VERCEL_PROJECTS_TABLE}")[0]
    assert rows == 3


def test_a_cursor_that_does_not_move_aborts(dlt_mod, tmp_path):
    # One item a page means the overlap re-reads the same item forever.
    fake = _fleet()
    fake.page_size = 1
    with pytest.raises(PipelineStepFailed, match="did not move"):
        _run_sync(dlt_mod, tmp_path, fake)


def test_a_404_on_a_later_page_aborts(dlt_mod, tmp_path):
    # Treating it as "project deleted" would keep only the pages read before it.
    fake = _fleet()
    first = _run_sync(dlt_mod, tmp_path, fake, project_ids=["web"])
    before = _read(first, VERCEL_DEPLOYMENTS_TABLE)

    fake.deployments.append(_deployment("dpl_ok3", "web", minutes_ago=40))  # a second page
    later_page = lambda path, params: path == "/v7/deployments" and "until" in params  # noqa: E731
    fake.fail = lambda path, params: 404 if later_page(path, params) else None
    with pytest.raises(PipelineStepFailed):
        _run_sync(dlt_mod, tmp_path, fake, project_ids=["web"])
    assert _read(_pipeline(dlt_mod, tmp_path), VERCEL_DEPLOYMENTS_TABLE) == before


def _session_on(fake):
    session = _default_session()
    session.mount("https://", fake)
    return session


def test_a_429_stops_the_run_at_once(dlt_mod, tmp_path):
    # Waiting out the limit would hold cognee's staging lock, which every other
    # dlt ingestion in the process waits on. The run stops instead.
    fake = _fleet()
    before = {table: _read(_run_sync(dlt_mod, tmp_path, fake), table) for table in _TABLES}

    fake.calls.clear()
    fake.retry_after = "1000"
    fake.fail = lambda path, params: 429 if path == "/v7/deployments" else None
    source = vercel_source(token="test-token", service=_session_on(fake), include_build_logs=True)
    started = time.monotonic()
    with pytest.raises(PipelineStepFailed, match=r"rate limited \(429\)"):
        _pipeline(dlt_mod, tmp_path).run(source)

    assert time.monotonic() - started < 10, "no Retry-After wait"
    assert len(_deployment_calls(fake)) == 1, "not retried"
    assert source.cognee_sync_stats["failed_rate_limit"] == 1
    assert {table: _read(_pipeline(dlt_mod, tmp_path), table) for table in _TABLES} == before


def test_a_transient_5xx_gets_one_short_retry(dlt_mod, tmp_path, monkeypatch):
    # A run of a few hundred requests should not fail on one blip, but any wait
    # is spent inside cognee's staging lock, so it is one retry, a few seconds.
    slept = _record_sleeps(monkeypatch)
    fake = _fleet()
    fake.retry_after = "1000"
    failed = []

    def once_on_page_two(path, params):
        if path == "/v7/deployments" and "until" in params and not failed:
            failed.append(path)
            return 503
        return None

    fake.fail = once_on_page_two
    pipeline = _run_sync(dlt_mod, tmp_path, fake, service=_session_on(fake))
    assert set(_read(pipeline, VERCEL_DEPLOYMENTS_TABLE)) == {"dpl_ok1", "dpl_ok2", "dpl_err"}
    assert failed and slept == [5], "one retry, its 1000 s Retry-After capped at 5 s"


def test_a_5xx_that_persists_stops_the_run_after_one_retry(dlt_mod, tmp_path, monkeypatch):
    slept = _record_sleeps(monkeypatch)
    fake = _fleet()
    before = {table: _read(_run_sync(dlt_mod, tmp_path, fake), table) for table in _TABLES}

    fake.calls.clear()
    fake.retry_after = "1000"
    fake.fail = lambda path, params: 503 if path == "/v7/deployments" else None
    with pytest.raises(PipelineStepFailed, match="503"):
        _run_sync(dlt_mod, tmp_path, fake, service=_session_on(fake))
    assert len(_deployment_calls(fake)) == 2, "one retry"
    assert slept == [5]
    assert {table: _read(_pipeline(dlt_mod, tmp_path), table) for table in _TABLES} == before


def test_a_429_is_never_retried(dlt_mod, tmp_path, monkeypatch):
    slept = _record_sleeps(monkeypatch)
    fake = _fleet()
    fake.fail = lambda path, params: 429 if path == "/v7/deployments" else None
    with pytest.raises(PipelineStepFailed, match=r"rate limited \(429\)"):
        _run_sync(dlt_mod, tmp_path, fake, service=_session_on(fake))
    assert len(_deployment_calls(fake)) == 1 and slept == []


def test_the_default_session_never_retries_a_429(monkeypatch):
    # Not only through the response hooks: the session itself leaves 429 out of
    # the codes it retries, so a request without the hooks is not retried either.
    slept = _record_sleeps(monkeypatch)
    fake = _fleet()
    fake.retry_after = "1000"
    fake.fail = lambda path, params: 429
    session = _session_on(fake)
    assert session.get("https://api.vercel.com/v10/projects").status_code == 429
    assert len(fake.calls) == 1 and slept == []

    fake.fail = lambda path, params: 503
    fake.calls.clear()
    assert session.get("https://api.vercel.com/v10/projects").status_code == 503
    assert len(fake.calls) == 2 and slept == [5]


def test_a_429_on_the_events_call_stops_the_run(dlt_mod, tmp_path, monkeypatch):
    # The events call has its own response hooks, so it gets its own test.
    slept = _record_sleeps(monkeypatch)
    fake = _fleet()
    fake.retry_after = "1000"
    fake.fail = lambda path, params: 429 if path.endswith("/events") else None
    source = vercel_source(token="test-token", service=_session_on(fake), include_build_logs=True)
    with pytest.raises(PipelineStepFailed, match=r"rate limited \(429\)"):
        _pipeline(dlt_mod, tmp_path).run(source)
    assert len(fake.event_paths()) == 1 and slept == []
    assert source.cognee_sync_stats["failed_rate_limit"] == 1


def test_a_dropped_connection_gets_one_short_retry(dlt_mod, tmp_path, monkeypatch):
    slept = _record_sleeps(monkeypatch)
    fake = _fleet()
    dropped = []

    def drop_page_two_once(path, params):
        if path == "/v7/deployments" and "until" in params and not dropped:
            dropped.append(path)
            return requests.ConnectionError("connection reset by peer")
        return None

    fake.fail = drop_page_two_once
    pipeline = _run_sync(dlt_mod, tmp_path, fake, service=_session_on(fake))
    assert set(_read(pipeline, VERCEL_DEPLOYMENTS_TABLE)) == {"dpl_ok1", "dpl_ok2", "dpl_err"}
    assert dropped and len(slept) == 1 and slept[0] <= 5


def _fail_first_try_of(*paths):
    """A fail() that answers 503 to the first request of each path, then lets it through."""
    failed = set()

    def fail(path, params):
        if path in paths and path not in failed:
            failed.add(path)
            return 503
        return None

    return fail


def test_a_run_gets_one_retry_in_total(dlt_mod, tmp_path, monkeypatch):
    # Every wait is spent inside cognee's staging lock, so the retry is for the
    # whole run, not one per request: a second blip in the same run stops it.
    slept = _record_sleeps(monkeypatch)
    fake = _fleet()
    fake.retry_after = "1000"
    fake.fail = _fail_first_try_of("/v10/projects", "/v7/deployments")
    with pytest.raises(PipelineStepFailed, match="503"):
        _run_sync(dlt_mod, tmp_path, fake, service=_session_on(fake))
    assert slept == [5], "one wait for the run"
    assert len(_deployment_calls(fake)) == 1, "the second blip of the run is not retried"


def test_each_run_starts_with_its_own_retry(dlt_mod, tmp_path, monkeypatch):
    slept = _record_sleeps(monkeypatch)
    fake = _fleet()
    fake.retry_after = "1000"
    source = vercel_source(token="test-token", service=_session_on(fake))
    pipeline = _pipeline(dlt_mod, tmp_path)
    for _ in range(2):  # one blip per run, retried in both runs
        fake.fail = _fail_first_try_of("/v10/projects")
        pipeline.run(source)
    assert slept == [5, 5]
    assert set(_read(pipeline, VERCEL_PROJECTS_TABLE)) == {"prj_web", "prj_api"}


def test_a_request_that_times_out_is_not_retried(dlt_mod, tmp_path, monkeypatch):
    # A hang already cost 30 s under the staging lock; a retry would double it.
    slept = _record_sleeps(monkeypatch)
    fake = _fleet()
    fake.fail = lambda path, params: (
        requests.ReadTimeout("read timed out") if path == "/v10/projects" else None
    )
    with pytest.raises(PipelineStepFailed, match="ReadTimeout failed on /v10/projects"):
        _run_sync(dlt_mod, tmp_path, fake, service=_session_on(fake))
    assert len([call for call in fake.calls if call[0] == "/v10/projects"]) == 1
    assert slept == []


def _error_in(exc: BaseException):
    """The VercelHTTPError somewhere in a pipeline error's chain."""
    from cognee_community_connector_vercel import VercelHTTPError

    seen = getattr(exc, "exception", None) or exc
    while seen is not None:
        if isinstance(seen, VercelHTTPError):
            return seen
        seen = seen.__cause__ or seen.__context__
    return None


@pytest.mark.parametrize("status", [401, 403])
def test_a_rejected_token_raises_a_named_error_without_the_query(dlt_mod, tmp_path, status):
    from cognee_community_connector_vercel import VercelAuthError

    fake = _fleet()
    fake.fail = lambda path, params: status if path == "/v10/projects" else None
    source = vercel_source(token="test-token", team_id="team_1", service=fake.session())
    with pytest.raises(PipelineStepFailed) as raised:
        _pipeline(dlt_mod, tmp_path).run(source)

    error = _error_in(raised.value)
    assert isinstance(error, VercelAuthError) and error.status == status
    assert f"HTTP {status} on /v10/projects: check the token" in str(raised.value)
    # The team id is part of the table name by design; the query must not show.
    assert "teamId=" not in str(raised.value) and "test-token" not in str(raised.value)
    assert source.cognee_sync_stats["failed_auth"] == 1


def test_a_server_error_names_the_status_and_endpoint_only(dlt_mod, tmp_path):
    fake = _fleet()
    fake.fail = lambda path, params: 500 if path == "/v7/deployments" else None
    with pytest.raises(PipelineStepFailed, match="HTTP 500 on /v7/deployments") as raised:
        _run_sync(dlt_mod, tmp_path, fake, team_id="team_1", project_ids=["web"])
    assert _error_in(raised.value).status == 500
    for secret in ("teamId=", "projectId=", "since=", "test-token"):
        assert secret not in str(raised.value)


def test_each_request_times_out_after_30_seconds(dlt_mod, tmp_path):
    fake = _fleet()
    _run_sync(dlt_mod, tmp_path, fake, service=_session_on(fake))
    assert fake.timeouts and set(fake.timeouts) == {30}


def test_a_429_on_a_later_page_stops_the_run(dlt_mod, tmp_path):
    fake = _fleet()
    before = {table: _read(_run_sync(dlt_mod, tmp_path, fake), table) for table in _TABLES}
    fake.fail = lambda path, params: (
        429 if path == "/v7/deployments" and "until" in params else None
    )
    with pytest.raises(PipelineStepFailed, match=r"rate limited \(429\)"):
        _run_sync(dlt_mod, tmp_path, fake)
    assert {table: _read(_pipeline(dlt_mod, tmp_path), table) for table in _TABLES} == before


def test_counts_are_cleared_when_a_run_fails(dlt_mod, tmp_path):
    fake = _fleet()
    source = vercel_source(token="test-token", service=fake.session())
    pipeline = _pipeline(dlt_mod, tmp_path)
    pipeline.run(source)
    assert source.cognee_sync_stats["deployments"] == 3

    fake.fail = lambda path, params: 500 if path == "/v7/deployments" else None
    with pytest.raises(PipelineStepFailed):
        pipeline.run(source)
    assert "deployments" not in source.cognee_sync_stats


def test_an_expired_build_log_keeps_its_document(dlt_mod, tmp_path):
    # Vercel answers 410 once it no longer keeps a build's output. The deployment
    # is still listed, so the log row from the last load is loaded again,
    # unchanged, and cognee keeps the document.
    fake = _fleet()
    before = _read(_run_sync(dlt_mod, tmp_path, fake), VERCEL_BUILD_LOGS_TABLE)
    assert set(before) == {"dpl_err"}

    fake.fail = lambda path, params: 410 if path.endswith("/events") else None
    pipeline = _run_sync(dlt_mod, tmp_path, fake)
    assert _read(pipeline, VERCEL_BUILD_LOGS_TABLE) == before
    assert "dpl_err" in _read(pipeline, VERCEL_DEPLOYMENTS_TABLE)

    # Once the deployment itself is gone, so is the kept log.
    fake.deployments = [item for item in fake.deployments if item["uid"] != "dpl_err"]
    pipeline = _run_sync(dlt_mod, tmp_path, fake)
    assert _read(pipeline, VERCEL_BUILD_LOGS_TABLE) == {}


def test_an_expired_log_without_an_inspector_url_keeps_its_document(dlt_mod, tmp_path):
    # Vercel types inspectorUrl as nullable. The `url` column is declared, so a
    # load where no failed build has one still has `url` (None), and the kept row
    # comes back exactly as it was loaded, url included.
    fake = _fleet()
    for deployment in fake.deployments:
        if deployment["uid"] == "dpl_err":
            del deployment["inspectorUrl"]
    first = _run_sync(dlt_mod, tmp_path, fake)
    before = _read(first, VERCEL_BUILD_LOGS_TABLE)
    assert set(before) == {"dpl_err"} and before["dpl_err"]["url"] is None

    fake.fail = lambda path, params: 410 if path.endswith("/events") else None
    for _ in range(2):  # and again on the next run, from the row it kept
        pipeline = _run_sync(dlt_mod, tmp_path, fake)
        assert _read(pipeline, VERCEL_BUILD_LOGS_TABLE) == before


def test_rows_without_a_url_keep_their_columns_when_a_later_load_has_one(dlt_mod, tmp_path):
    # dlt creates no column that is None in every row. If `url` appeared only
    # when a later load had an inspectorUrl, every older row would read back with
    # a new NULL column, and cognee, which hashes every column, would re-ingest
    # rows that did not change. The column is declared, so it is there from the
    # first load.
    fake = _fleet()
    for deployment in fake.deployments:
        del deployment["inspectorUrl"]
    first = {table: _read(_run_sync(dlt_mod, tmp_path, fake), table) for table in _TABLES}
    assert all(row["url"] is None for row in first[VERCEL_DEPLOYMENTS_TABLE].values())

    fake.deployments.append(_deployment("dpl_new", "web", minutes_ago=1))  # has a URL
    second = _run_sync(dlt_mod, tmp_path, fake)
    for table in _TABLES:
        rows = _read(second, table)
        assert {key: rows[key] for key in first[table]} == first[table], table


def test_an_expired_log_with_no_earlier_load_is_skipped(dlt_mod, tmp_path):
    fake = _fleet()
    fake.fail = lambda path, params: 410 if path.endswith("/events") else None
    source = vercel_source(token="test-token", service=fake.session(), include_build_logs=True)
    pipeline = _pipeline(dlt_mod, tmp_path)
    pipeline.run(source)
    assert source.cognee_sync_stats["build_logs"] == 0
    assert "dpl_err" in _read(pipeline, VERCEL_DEPLOYMENTS_TABLE)


def test_a_secret_printed_by_a_build_stays_out_by_default(dlt_mod, tmp_path):
    # Vercel only redacts sensitive values of 32 characters or more, so a build
    # can print a shorter one into its log. Logs are only read when asked for.
    fake = _fleet()
    fake.logs["dpl_err"] = _log("Installing dependencies", f"DB_URL={CANARY_ENV}")
    _run_sync(dlt_mod, tmp_path / "default", fake, include_build_logs=False)

    _, written = _everything_written(tmp_path / "default")
    assert CANARY_ENV not in written
    assert fake.event_paths() == []

    # Asked for, the log is loaded as Vercel returns it. The README says so.
    _run_sync(dlt_mod, tmp_path / "logs", fake, include_build_logs=True)
    _, written = _everything_written(tmp_path / "logs")
    assert CANARY_ENV in written


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


async def _fake_embed_text(self, text):
    """Zero vectors in whichever shape the installed cognee returns.

    cognee 1.6.x returns a list of vectors; later versions return an
    ``EmbeddingResult`` (a list that also carries a token count).
    """
    vectors = [[0.0] * self.get_vector_size() for _ in text]
    try:
        from cognee.infrastructure.databases.vector.embeddings.EmbeddingEngine import (
            EmbeddingResult,
        )
    except ImportError:
        return vectors
    return EmbeddingResult(vectors, tokens=0)


def _cognee_offline(tmp_path, monkeypatch):
    """Point cognee at tmp_path with no credentials, the way the graph test does."""
    import cognee

    monkeypatch.setenv("COGNEE_SKIP_CONNECTION_TEST", "true")
    monkeypatch.setenv("ENABLE_BACKEND_ACCESS_CONTROL", "false")
    monkeypatch.setenv("DLT_DATA_DIR", str(tmp_path / "dlt"))
    monkeypatch.setenv("PIPELINES_DIR", str(tmp_path / "dlt" / "pipelines"))
    cognee.config.data_root_directory(str(tmp_path / "data"))
    cognee.config.system_root_directory(str(tmp_path / "system"))
    cognee.config.set_relational_db_config({"db_provider": "sqlite"})
    return cognee


def test_two_teams_in_one_dataset_keep_their_documents(tmp_path, monkeypatch):
    # Each team gets its own tables by default, so a sync of one team never
    # replaces, and orphan-cleans, the other team's documents.
    pytest.importorskip("dlt")
    import asyncio

    cognee = _cognee_offline(tmp_path, monkeypatch)
    team_a = FakeVercel(
        account="team_A",
        projects=[],
        deployments=[_deployment("dpl_a1", "alpha"), _deployment("dpl_a2", "alpha")],
    )
    team_b = FakeVercel(projects=[], deployments=[_deployment("dpl_b1", "bravo")], account="team_B")

    async def sync(fake, team):
        # `session` is the older name of `service`; both are accepted.
        source = vercel_source(token="test-token", team_id=team, session=fake.session())
        await cognee.add(source, dataset_name="vercel_teams")

    async def count():
        datasets = await cognee.datasets.list_datasets()
        found = next(item for item in datasets if item.name == "vercel_teams")
        return len(await cognee.datasets.list_data(found.id))

    async def scenario():
        await cognee.prune.prune_data()
        await cognee.prune.prune_system(metadata=True)
        try:
            await sync(team_a, "team_A")
            await sync(team_b, "team_B")
            assert await count() == 3
            team_a.deployments = team_a.deployments[:1]
            await sync(team_a, "team_A")
            assert await count() == 2, "only team A's deleted deployment may go"
        finally:
            await cognee.prune.prune_data()
            await cognee.prune.prune_system(metadata=True)

    asyncio.run(scenario())


def test_cognee_keeps_an_expired_log(tmp_path, monkeypatch):
    # The kept log is read back from cognee's own staging database, so check it
    # there: after the events call starts answering 410, no document goes.
    pytest.importorskip("dlt")
    import asyncio

    cognee = _cognee_offline(tmp_path, monkeypatch)
    fake = _fleet()

    async def sync():
        source = vercel_source(token="test-token", service=fake.session(), include_build_logs=True)
        await cognee.add(source, dataset_name="vercel_expiry")
        return source.cognee_sync_stats

    async def count():
        datasets = await cognee.datasets.list_datasets()
        found = next(item for item in datasets if item.name == "vercel_expiry")
        return len(await cognee.datasets.list_data(found.id))

    async def scenario():
        await cognee.prune.prune_data()
        await cognee.prune.prune_system(metadata=True)
        try:
            assert (await sync())["build_logs"] == 1
            documents = await count()
            fake.fail = lambda path, params: 410 if path.endswith("/events") else None
            assert (await sync())["build_logs"] == 1, "the expired log is kept"
            assert await count() == documents
        finally:
            await cognee.prune.prune_data()
            await cognee.prune.prune_system(metadata=True)

    asyncio.run(scenario())


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

    monkeypatch.setattr(add_data_points_module, "index_data_points", _noop_index)
    monkeypatch.setattr(add_data_points_module, "index_graph_edges", _noop_index)
    monkeypatch.setattr(LLMGateway, "acreate_structured_output", _mock_structured_output)
    monkeypatch.setattr(LiteLLMEmbeddingEngine, "embed_text", _fake_embed_text)

    fake = FakeVercel(
        projects=[],
        deployments=[
            _deployment("dpl_alpha", ALPHA, minutes_ago=20),
            _deployment("dpl_bravo", BRAVO, state="ERROR", minutes_ago=10),
        ],
        logs={"dpl_bravo": _log(f"{BRAVO} build failed")},
    )

    async def sync():
        source = vercel_source(token="test-token", service=fake.session(), include_build_logs=True)
        await cognee.add(source, dataset_name=dataset)
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
