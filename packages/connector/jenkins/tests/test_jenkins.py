"""Unit tests for the Jenkins connector.

The Jenkins JSON API is mocked by ``FakeJenkins`` (no network, no credentials),
so these run in CI. Coverage:

  - job paths are built from full names, with folders and URL-encoding
  - base URL validation rejects non-http(s) URLs and embedded credentials
  - every JSON request is bounded with ``tree=``
  - first sync ingests jobs and the most recent builds, and records the cursor
  - incremental sync fetches only builds above the cursor; no change is a no-op
  - a running build is skipped and holds the cursor until it completes
  - console logs are fetched only for failing builds, tail-truncated and redacted
  - parameter default values are never ingested
  - builds Jenkins discarded and jobs that vanished become hard-delete markers
  - an empty sweep does not mass-delete
  - folders are discovered recursively, bounded by max_folder_depth
  - the dlt resource is wired with merge + id PK + the hard_delete column
  - a real dlt merge removes the marked rows (end-to-end forget-on-delete)
"""

import re
from urllib.parse import unquote

import pytest

from cognee_community_connector_jenkins.jenkins import (
    _job_path,
    _validate_base_url,
    build_id,
    jenkins_source,
    job_id,
    redact,
    sync_jenkins,
)

BASE_URL = "https://ci.example.com"


# ---------------------------------------------------------------------------
# Fake Jenkins
# ---------------------------------------------------------------------------
class _Resp:
    def __init__(self, status=200, payload=None, body=b""):
        self.status_code = status
        self._payload = payload
        self._body = body

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self):
        return self._payload

    def iter_content(self, chunk_size=8192):
        for i in range(0, len(self._body), chunk_size):
            yield self._body[i : i + chunk_size]

    def close(self):
        pass


def _build(number, result="SUCCESS", *, building=False, log="", cause="Started by timer"):
    return {
        "number": number,
        "result": None if building else result,
        "building": building,
        "timestamp": 1_700_000_000_000 + number,
        "duration": 1000 * number,
        "displayName": f"#{number}",
        "description": None,
        "actions": [{"causes": [{"shortDescription": cause}]}, {}],
        "_log": log,
    }


class FakeJenkins:
    """Minimal stand-in for a ``requests`` session talking to the Jenkins JSON API.

    ``tree`` maps folder full names ("" = root) to child names; ``jobs`` maps job
    full names to a dict with optional ``description``/``params``/``buildable`` and a
    ``builds`` list built with ``_build``.
    """

    def __init__(self, jobs, folders=None, fail=None):
        self.jobs = jobs
        self.folders = folders if folders is not None else {"": sorted(jobs)}
        self.fail = fail or set()
        self.calls = []

    def _resolve(self, url):
        assert url.startswith(BASE_URL), url
        path = url[len(BASE_URL) :]
        names = [unquote(s) for s in re.findall(r"/job/([^/]+)", path)]
        rest = re.sub(r"(/job/[^/]+)+", "", path)
        return "/".join(names), rest

    def get(self, url, params=None, stream=False, timeout=None):
        params = params or {}
        self.calls.append((url, params))
        name, rest = self._resolve(url)
        if name in self.fail:
            return _Resp(status=500)

        if rest == "/api/json":
            assert "tree" in params, f"unbounded request: {url}"
            if name in self.folders:
                children = []
                for child in self.folders[name]:
                    full = f"{name}/{child}" if name else child
                    item = {"name": child, "fullName": full, "_class": "x"}
                    if full in self.folders:
                        item["jobs"] = [{"name": "?"}]
                    children.append(item)
                return _Resp(payload={"jobs": children})
            job = self.jobs.get(name)
            if job is None:
                return _Resp(status=404)
            builds = job.get("builds", [])
            return _Resp(
                payload={
                    "name": name.rsplit("/", 1)[-1],
                    "fullName": name,
                    "description": job.get("description"),
                    "buildable": job.get("buildable", True),
                    "color": "blue",
                    "property": [
                        {},
                        {
                            "parameterDefinitions": [
                                {
                                    "name": p["name"],
                                    "type": p.get("type", "StringParameterDefinition"),
                                    "description": p.get("description", ""),
                                    # Real Jenkins never returns this with our tree,
                                    # but make sure it would not leak if it did.
                                    "defaultParameterValue": {"value": "s3cr3t"},
                                }
                                for p in job.get("params", [])
                            ]
                        },
                    ],
                    "lastBuild": {"number": builds[-1]["number"]} if builds else None,
                    "allBuilds": [{"number": b["number"]} for b in reversed(builds)],
                }
            )

        m = re.fullmatch(r"/(\d+)/(api/json|consoleText)", rest)
        if m:
            job = self.jobs.get(name) or {}
            build = next((b for b in job.get("builds", []) if b["number"] == int(m.group(1))), None)
            if build is None:
                return _Resp(status=404)
            if m.group(2) == "consoleText":
                assert stream, "console logs must be streamed"
                return _Resp(body=build["_log"].encode())
            assert "tree" in params, f"unbounded request: {url}"
            return _Resp(payload={k: v for k, v in build.items() if not k.startswith("_")})

        raise AssertionError(f"unexpected URL: {url}")

    def urls(self, suffix):
        return [url for url, _ in self.calls if url.endswith(suffix)]


def _ids(rows):
    return sorted(r["id"] for r in rows if not r.get("_deleted"))


def _deleted(rows):
    return sorted(r["id"] for r in rows if r.get("_deleted"))


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------
def test_job_path_handles_folders_and_encodes_segments():
    assert _job_path("nightly") == "/job/nightly/"
    assert _job_path("team/backend/main") == "/job/team/job/backend/job/main/"
    assert _job_path("feature/a b#1") == "/job/feature/job/a%20b%231/"
    with pytest.raises(ValueError):
        _job_path("/")


def test_base_url_validation():
    assert _validate_base_url("https://ci.example.com/jenkins/") == "https://ci.example.com/jenkins"
    with pytest.raises(ValueError, match="http"):
        _validate_base_url("ftp://ci.example.com")
    with pytest.raises(ValueError, match="credentials"):
        _validate_base_url("https://admin:token@ci.example.com")


def test_redact_masks_credential_like_values_only():
    text = "TOKEN=abc123 password: hunter2\nbuild ok, tokens counted: 3 apikey=xyz"
    out = redact(text)
    assert "abc123" not in out and "hunter2" not in out and "xyz" not in out
    assert "TOKEN=****" in out and "password: ****" in out
    assert "build ok" in out


def test_redact_masks_keywords_inside_identifiers():
    # Found against a real Jenkins: build scripts print env-style variable names.
    text = "DEPLOY_TOKEN=supersecret123\nAWS_SECRET_ACCESS_KEY: wJalr\ndb-password=pw1\nok"
    out = redact(text)
    for secret in ("supersecret123", "wJalr", "pw1"):
        assert secret not in out
    assert "DEPLOY_TOKEN=****" in out and "AWS_SECRET_ACCESS_KEY: ****" in out
    assert out.endswith("ok")


# ---------------------------------------------------------------------------
# sync_jenkins — first sync / incremental / running builds
# ---------------------------------------------------------------------------
def test_first_sync_ingests_jobs_and_recent_builds_and_records_state():
    fake = FakeJenkins(
        {
            "api": {
                "description": "Backend API",
                "params": [{"name": "BRANCH"}],
                "builds": [_build(n) for n in range(1, 6)],
            }
        }
    )
    state = {}
    rows = list(sync_jenkins(fake, BASE_URL, state, max_builds_per_job=3))

    assert _ids(rows) == [build_id("api", 3), build_id("api", 4), build_id("api", 5), job_id("api")]
    job = next(r for r in rows if r["kind"] == "job")
    assert job["url"] == f"{BASE_URL}/job/api/"
    assert "Backend API" in job["content"] and "BRANCH" in job["content"]
    assert "s3cr3t" not in str(rows)  # parameter defaults are never ingested
    build = next(r for r in rows if r["id"] == build_id("api", 5))
    assert build["result"] == "SUCCESS"
    assert build["url"] == f"{BASE_URL}/job/api/5/"
    assert "Started by timer" in build["content"]
    assert state["jobs"]["api"]["last_build"] == 5
    assert state["jobs"]["api"]["builds"] == [3, 4, 5]


def test_older_builds_skipped_on_first_sync_are_not_backfilled_later():
    fake = FakeJenkins({"api": {"builds": [_build(n) for n in range(1, 6)]}})
    state = {}
    list(sync_jenkins(fake, BASE_URL, state, max_builds_per_job=2))
    fake.jobs["api"]["builds"].append(_build(6))
    rows = list(sync_jenkins(fake, BASE_URL, state, max_builds_per_job=2))
    assert _ids(rows) == [build_id("api", 6), job_id("api")]  # job row changed: lastBuild moved


def test_incremental_fetches_only_new_builds():
    fake = FakeJenkins({"api": {"builds": [_build(1), _build(2)]}})
    state = {}
    list(sync_jenkins(fake, BASE_URL, state))
    fake.calls.clear()
    fake.jobs["api"]["builds"].append(_build(3))

    rows = list(sync_jenkins(fake, BASE_URL, state))

    assert build_id("api", 3) in _ids(rows)
    assert build_id("api", 1) not in _ids(rows) and build_id("api", 2) not in _ids(rows)
    assert fake.urls("/1/api/json") == [] and fake.urls("/2/api/json") == []
    assert state["jobs"]["api"]["last_build"] == 3


def test_no_changes_is_a_noop():
    fake = FakeJenkins({"api": {"builds": [_build(1)]}})
    state = {}
    list(sync_jenkins(fake, BASE_URL, state))
    assert list(sync_jenkins(fake, BASE_URL, state)) == []


def test_running_build_holds_the_cursor_until_it_completes():
    fake = FakeJenkins({"api": {"builds": [_build(1), _build(2, building=True), _build(3)]}})
    state = {}
    rows = list(sync_jenkins(fake, BASE_URL, state))
    assert build_id("api", 2) not in _ids(rows)
    assert build_id("api", 3) in _ids(rows)  # later finished builds are not delayed
    assert state["jobs"]["api"]["last_build"] == 1

    fake.jobs["api"]["builds"][1] = _build(2, "FAILURE", log="boom")
    fake.calls.clear()
    rows = list(sync_jenkins(fake, BASE_URL, state))
    assert _ids(rows) == [build_id("api", 2)]
    assert fake.urls("/3/api/json") == []  # build 3 is not fetched again
    assert state["jobs"]["api"]["last_build"] == 3


# ---------------------------------------------------------------------------
# Console logs
# ---------------------------------------------------------------------------
def test_console_log_only_for_failed_builds_and_tail_truncated_and_redacted():
    long_log = "x" * 5000 + "\nERROR: deploy failed, token=abc123\n"
    fake = FakeJenkins(
        {"api": {"builds": [_build(1, "SUCCESS", log="fine"), _build(2, "FAILURE", log=long_log)]}}
    )
    rows = list(sync_jenkins(fake, BASE_URL, {}, max_log_bytes=200))

    assert fake.urls("/1/consoleText") == []  # successful builds: no log download
    failed = next(r for r in rows if r["id"] == build_id("api", 2))
    assert "ERROR: deploy failed" in failed["content"]
    assert "abc123" not in failed["content"] and "token=****" in failed["content"]
    assert failed["log_truncated"] is True
    assert "Console log (tail)" in failed["content"]
    assert len(failed["content"]) < 400


def test_log_results_can_include_unstable_builds():
    fake = FakeJenkins({"api": {"builds": [_build(1, "UNSTABLE", log="2 tests failed")]}})
    rows = list(sync_jenkins(fake, BASE_URL, {}, log_results=["FAILURE", "unstable"]))
    assert "2 tests failed" in rows[-1]["content"]


# ---------------------------------------------------------------------------
# Forget-on-delete
# ---------------------------------------------------------------------------
def test_discarded_builds_become_hard_delete_markers():
    fake = FakeJenkins({"api": {"builds": [_build(1), _build(2), _build(3)]}})
    state = {}
    list(sync_jenkins(fake, BASE_URL, state))
    fake.jobs["api"]["builds"] = [_build(2), _build(3)]  # build 1 rotated out

    rows = list(sync_jenkins(fake, BASE_URL, state))

    assert _deleted(rows) == [build_id("api", 1)]
    assert state["jobs"]["api"]["builds"] == [2, 3]


def test_deleted_job_forgets_the_job_and_all_its_builds():
    fake = FakeJenkins({"api": {"builds": [_build(1), _build(2)]}, "web": {"builds": [_build(1)]}})
    state = {}
    list(sync_jenkins(fake, BASE_URL, state))
    del fake.jobs["web"]
    fake.folders = {"": ["api"]}

    rows = list(sync_jenkins(fake, BASE_URL, state))

    assert _deleted(rows) == [build_id("web", 1), job_id("web")]
    assert set(state["jobs"]) == {"api"}


def test_explicit_job_that_returns_404_is_forgotten():
    fake = FakeJenkins({"api": {"builds": [_build(1)]}, "web": {"builds": [_build(1)]}})
    state = {}
    list(sync_jenkins(fake, BASE_URL, state, job_names=["api", "web"]))
    del fake.jobs["web"]
    rows = list(sync_jenkins(fake, BASE_URL, state, job_names=["api", "web"]))
    assert _deleted(rows) == [build_id("web", 1), job_id("web")]


def test_empty_sweep_does_not_mass_delete_and_preserves_state():
    fake = FakeJenkins({"api": {"builds": [_build(1)]}})
    state = {}
    list(sync_jenkins(fake, BASE_URL, state))
    before = dict(state["jobs"])
    fake.folders = {"": []}

    rows = list(sync_jenkins(fake, BASE_URL, state))

    assert rows == []
    assert state["jobs"] == before


def test_transient_error_aborts_without_touching_state():
    fake = FakeJenkins({"api": {"builds": [_build(1)]}})
    state = {}
    list(sync_jenkins(fake, BASE_URL, state))
    before = {k: dict(v) for k, v in state["jobs"].items()}
    fake.fail = {"api"}
    with pytest.raises(RuntimeError):
        list(sync_jenkins(fake, BASE_URL, state))
    assert state["jobs"] == before


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------
def test_jobs_in_folders_are_discovered_recursively_and_depth_is_capped():
    jobs = {"top": {}, "team/svc": {}, "team/deep/inner": {}}
    folders = {"": ["team", "top"], "team": ["deep", "svc"], "team/deep": ["inner"]}

    rows = list(sync_jenkins(FakeJenkins(jobs, folders), BASE_URL, {}))
    assert _ids(rows) == [job_id("team/deep/inner"), job_id("team/svc"), job_id("top")]

    rows = list(sync_jenkins(FakeJenkins(jobs, folders), BASE_URL, {}, max_folder_depth=1))
    assert _ids(rows) == [job_id("team/svc"), job_id("top")]


def test_folder_job_urls_are_built_from_the_full_name():
    fake = FakeJenkins({"team/svc": {"builds": [_build(1, "FAILURE", log="x")]}}, {})
    list(sync_jenkins(fake, BASE_URL, {}, job_names=["team/svc"]))
    assert fake.urls("/job/team/job/svc/1/consoleText")


# ---------------------------------------------------------------------------
# jenkins_source — dlt wiring
# ---------------------------------------------------------------------------
def test_jenkins_source_resource_is_configured_for_merge_and_hard_delete():
    pytest.importorskip("dlt")
    resource = jenkins_source(base_url=BASE_URL, session=FakeJenkins({}))
    assert resource.name == "jenkins_records"

    schema = resource.compute_table_schema()
    write_disposition = schema.get("write_disposition")
    if isinstance(write_disposition, dict):
        write_disposition = write_disposition.get("disposition")
    assert write_disposition == "merge"
    assert schema["columns"]["id"].get("primary_key") is True
    assert schema["columns"]["_deleted"].get("hard_delete") is True


def test_jenkins_source_requires_credentials_or_session():
    pytest.importorskip("dlt")
    with pytest.raises(ValueError, match="username and api_token"):
        jenkins_source(base_url=BASE_URL)


# ---------------------------------------------------------------------------
# End-to-end: a real dlt merge acts on the hard-delete markers
# ---------------------------------------------------------------------------
def _read_ids(pipeline):
    with (
        pipeline.sql_client() as client,
        client.execute_query("SELECT id FROM jenkins_records") as cursor,
    ):
        return sorted(row[0] for row in cursor.fetchall())


def test_incremental_and_forget_on_delete_end_to_end_through_a_real_dlt_merge(tmp_path):
    dlt = pytest.importorskip("dlt")

    pipeline = dlt.pipeline(
        pipeline_name="jenkins_test",
        destination=dlt.destinations.sqlalchemy(f"sqlite:///{(tmp_path / 'j.db').as_posix()}"),
        dataset_name="ci",
        pipelines_dir=str(tmp_path / "state"),
    )
    fake = FakeJenkins(
        {
            "api": {"builds": [_build(1), _build(2, "FAILURE", log="ERROR boom")]},
            "web": {"builds": [_build(1)]},
        }
    )

    # Sync #1: both jobs and their builds land in the destination.
    pipeline.run(jenkins_source(base_url=BASE_URL, session=fake))
    assert _read_ids(pipeline) == [
        build_id("api", 1),
        build_id("api", 2),
        build_id("web", 1),
        job_id("api"),
        job_id("web"),
    ]

    # Sync #2: a new build on api, api #1 rotated out, the web job deleted.
    fake.jobs["api"]["builds"] = [_build(2, "FAILURE", log="ERROR boom"), _build(3)]
    del fake.jobs["web"]
    fake.folders = {"": ["api"]}
    fake.calls.clear()
    pipeline.run(jenkins_source(base_url=BASE_URL, session=fake))

    assert _read_ids(pipeline) == [build_id("api", 2), build_id("api", 3), job_id("api")]
    assert fake.urls("/2/api/json") == []  # dlt persisted the cursor between runs
