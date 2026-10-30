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
  - complete inventories automatically emit scoped hard deletes
  - a complete empty sweep deletes the last job
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
        self.closed = False

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self):
        return self._payload

    def iter_content(self, chunk_size=8192):
        for i in range(0, len(self._body), chunk_size):
            yield self._body[i : i + chunk_size]

    def close(self):
        self.closed = True


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
                    "allBuilds": self._build_page(builds, params["tree"]),
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

    @staticmethod
    def _build_page(builds, tree):
        match = re.search(r"allBuilds\[number\]\{(\d+),(\d+)\}", tree)
        assert match, "build-number enumeration must specify a range"
        start, end = map(int, match.groups())
        assert end - start <= 100
        return [{"number": b["number"]} for b in reversed(builds)][start:end]

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


def test_redact_mixed_credential_formats():
    samples = [
        ('{"api_key": "fixture-secret-value"}', "fixture-secret-value"),
        ("Authorization: Bearer example-fake-secret", "example-fake-secret"),
        ("curl -u demo:example-secret https://example.test", "demo:example-secret"),
        ("ghp_" + "A" * 24, "ghp_" + "A" * 24),
    ]
    for sample, forbidden in samples:
        masked = redact(sample)
        assert forbidden not in masked
        assert "****" in masked


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
    job = next(r for r in rows if r["id"] == job_id("api"))
    assert job["url"] == f"{BASE_URL}/job/api/"
    assert "Backend API" in job["content"] and "BRANCH" in job["content"]
    assert "s3cr3t" not in str(rows)  # parameter defaults are never ingested
    build = next(r for r in rows if r["id"] == build_id("api", 5))
    assert "SUCCESS" in build["title"]
    assert build["url"] == f"{BASE_URL}/job/api/5/"
    assert set(job) == {"id", "title", "content", "url", "_deleted"}
    assert set(build) == {"id", "title", "content", "url", "_deleted"}
    assert "Started by timer" in build["content"]
    assert state["jobs"]["api"]["last_build"] == 5
    assert state["jobs"]["api"]["builds"] == [3, 4, 5]


def test_older_builds_skipped_on_first_sync_are_not_backfilled_later():
    fake = FakeJenkins({"api": {"builds": [_build(n) for n in range(1, 6)]}})
    state = {}
    list(sync_jenkins(fake, BASE_URL, state, max_builds_per_job=2))
    fake.jobs["api"]["builds"].append(_build(6))
    rows = list(sync_jenkins(fake, BASE_URL, state, max_builds_per_job=2))
    assert _ids(rows) == [build_id("api", 6)]  # lastBuild alone cannot rehash the job document


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


def test_job_metadata_change_reemits_document_but_new_build_does_not():
    fake = FakeJenkins({"api": {"description": "Initial job", "builds": [_build(1)]}})
    state = {}
    first = list(sync_jenkins(fake, BASE_URL, state))
    job = next(row for row in first if row["id"] == job_id("api"))
    assert "Initial job" in job["content"]

    fake.jobs["api"]["builds"].append(_build(2))
    second = list(sync_jenkins(fake, BASE_URL, state))
    assert _ids(second) == [build_id("api", 2)]

    fake.jobs["api"]["description"] = "Updated job"
    third = list(sync_jenkins(fake, BASE_URL, state))
    assert _ids(third) == [job_id("api")]
    assert "Updated job" in third[0]["content"]


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
    assert set(failed) == {"id", "title", "content", "url", "_deleted"}
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


def test_explicit_job_404_preserves_state_instead_of_deleting_history():
    fake = FakeJenkins({"api": {"builds": [_build(1)]}, "web": {"builds": [_build(1)]}})
    state = {}
    list(sync_jenkins(fake, BASE_URL, state, job_names=["api", "web"]))
    before = {k: dict(v) for k, v in state["jobs"].items()}
    del fake.jobs["web"]
    with pytest.raises(RuntimeError, match="refusing possible deletes"):
        list(sync_jenkins(fake, BASE_URL, state, job_names=["api", "web"]))
    assert state["jobs"] == before


def test_absent_allbuilds_does_not_delete_known_builds():
    class MissingAllBuilds(FakeJenkins):
        def get(self, url, params=None, stream=False, timeout=None):
            result = super().get(url, params=params, stream=stream, timeout=timeout)
            if url.endswith("/api/json") and isinstance(result._payload, dict):
                result._payload.pop("allBuilds", None)
            return result

    fake = MissingAllBuilds({"api": {"builds": [_build(1), _build(2)]}})
    state = {}
    list(sync_jenkins(fake, BASE_URL, state))
    assert state["jobs"]["api"]["builds"] == [2]
    fake.jobs["api"]["builds"] = [_build(3)]
    rows = list(sync_jenkins(fake, BASE_URL, state))
    assert _deleted(rows) == []
    assert build_id("api", 3) in _ids(rows)
    assert state["jobs"]["api"]["builds"] == [2, 3]


def test_complete_empty_subfolder_deletes_its_previous_jobs():
    fake = FakeJenkins(
        {"team/api": {"builds": [_build(1)]}, "other": {"builds": [_build(1)]}},
        {"": ["team", "other"], "team": ["api"]},
    )
    state = {}
    list(sync_jenkins(fake, BASE_URL, state))
    fake.folders["team"] = []
    rows = list(sync_jenkins(fake, BASE_URL, state))
    assert _deleted(rows) == [build_id("team/api", 1), job_id("team/api")]
    assert set(state["jobs"]) == {"other"}


def test_missing_folder_listing_aborts_before_any_mass_delete():
    class MissingChildren(FakeJenkins):
        def get(self, url, params=None, stream=False, timeout=None):
            result = super().get(url, params=params, stream=stream, timeout=timeout)
            if "/job/team/api/json" in url and isinstance(result._payload, dict):
                result._payload.pop("jobs", None)
            return result

    jobs = {"team/api": {"builds": [_build(1)]}, "other": {"builds": [_build(1)]}}
    folders = {"": ["team", "other"], "team": ["api"]}
    state = {}
    list(sync_jenkins(FakeJenkins(jobs, folders), BASE_URL, state))
    before = {k: dict(v) for k, v in state["jobs"].items()}
    with pytest.raises(RuntimeError, match="missing jobs list"):
        list(sync_jenkins(MissingChildren(jobs, folders), BASE_URL, state))
    assert state["jobs"] == before


def test_complete_empty_sweep_deletes_last_job():
    fake = FakeJenkins({"api": {"builds": [_build(1)]}})
    state = {}
    list(sync_jenkins(fake, BASE_URL, state))
    fake.folders = {"": []}

    rows = list(sync_jenkins(fake, BASE_URL, state))

    assert _deleted(rows) == [build_id("api", 1), job_id("api")]
    assert state["jobs"] == {}


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

    with pytest.raises(RuntimeError, match="discovery incomplete"):
        list(sync_jenkins(FakeJenkins(jobs, folders), BASE_URL, {}, max_folder_depth=1))


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
    from cognee_community_connector_jenkins.jenkins import DOCUMENT_SOURCE_ATTR, PIPELINE_SCOPE_ATTR

    assert getattr(resource, DOCUMENT_SOURCE_ATTR) == "jenkins"
    assert getattr(resource, PIPELINE_SCOPE_ATTR) == "jenkins_records"
    second = jenkins_source(
        base_url=BASE_URL, session=FakeJenkins({}), resource_name="jenkins_other"
    )
    assert second.name == "jenkins_other"
    assert getattr(second, PIPELINE_SCOPE_ATTR) == "jenkins_other"

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
    pipeline.run(
        jenkins_source(
            base_url=BASE_URL,
            session=fake,
        )
    )
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
    pipeline.run(
        jenkins_source(
            base_url=BASE_URL,
            session=fake,
        )
    )

    assert _read_ids(pipeline) == [build_id("api", 2), build_id("api", 3), job_id("api")]
    assert fake.urls("/2/api/json") == []  # dlt persisted the cursor between runs


@pytest.mark.parametrize(
    "listing",
    [None, {}, [None], [{"number": "bad"}], [{"number": True}], [{"number": 2}, {"number": 2}]],
)
def test_malformed_build_listing_aborts_and_preserves_state(listing):
    class Broken(FakeJenkins):
        def get(self, url, **kwargs):
            response = super().get(url, **kwargs)
            if url.endswith("/job/api/api/json"):
                response._payload["allBuilds"] = listing
            return response

    state = {"jobs": {"api": {"last_build": 1, "builds": [1]}}}
    import copy

    before = copy.deepcopy(state)
    with pytest.raises(RuntimeError, match="build listing"):
        list(sync_jenkins(Broken({"api": {"builds": [_build(2)]}}), BASE_URL, state))
    assert state == before


def test_metadata_is_masked_even_when_console_masking_is_disabled():
    build = _build(1, cause="token=fixture-cause")
    build["description"] = "password=fixture-description"
    rows = list(
        sync_jenkins(FakeJenkins({"api": {"builds": [build]}}), BASE_URL, {}, redact_logs=False)
    )
    content = rows[-1]["content"]
    assert "fixture-cause" not in content
    assert "fixture-description" not in content
    assert "Cause: token=****" in content
    assert "Description: password=****" in content


def test_ten_thousand_builds_use_bounded_pages_and_incremental_stop():
    fake = FakeJenkins({"api": {"builds": [_build(n) for n in range(1, 10001)]}})
    state = {}
    rows = list(sync_jenkins(fake, BASE_URL, state))
    assert len(rows) == 21
    assert state["jobs"]["api"]["builds"] == list(range(9981, 10001))
    fake.calls.clear()
    assert list(sync_jenkins(fake, BASE_URL, state)) == []
    assert len(fake.calls) == 2  # discovery + one bounded build page
    fake.jobs["api"]["builds"] = [_build(n) for n in range(9990, 10002)]
    rows = list(sync_jenkins(fake, BASE_URL, state))
    assert _ids(rows) == ["build:api#10001"]
    assert _deleted(rows) == [build_id("api", n) for n in range(9981, 9990)]
    assert state["jobs"]["api"]["builds"] == list(range(9990, 10002))


def test_paging_catches_more_than_one_page_of_new_builds():
    fake = FakeJenkins({"api": {"builds": [_build(1)]}})
    state = {}
    list(sync_jenkins(fake, BASE_URL, state))
    fake.jobs["api"]["builds"] = [_build(n) for n in range(1, 252)]
    rows = list(sync_jenkins(fake, BASE_URL, state))
    assert len(rows) == 250
    assert state["jobs"]["api"]["last_build"] == 251


def test_optional_guard_retains_job_omitted_from_complete_discovery():
    fake = FakeJenkins({"api": {"builds": [_build(1)]}, "web": {"builds": [_build(1)]}})
    state = {}
    list(sync_jenkins(fake, BASE_URL, state))
    fake.folders = {"": ["api"]}
    rows = list(sync_jenkins(fake, BASE_URL, state, confirm_deleted=lambda *_: False))
    assert _deleted(rows) == []
    assert set(state["jobs"]) == {"api", "web"}


@pytest.mark.parametrize(
    "payload",
    [
        {"jobs": [None]},
        {"jobs": [{}]},
        {"jobs": [{"name": "team", "jobs": None}]},
        {"jobs": [], "truncated": True},
        {"jobs": [], "hasMore": True},
        {"jobs": [], "complete": False},
        {"jobs": [], "total": 10},
    ],
)
def test_malformed_or_partial_discovery_aborts(payload):
    class Broken(FakeJenkins):
        def get(self, url, **kwargs):
            if url == BASE_URL + "/api/json":
                return _Resp(payload=payload)
            return super().get(url, **kwargs)

    state = {}
    with pytest.raises(RuntimeError, match="discovery"):
        list(sync_jenkins(Broken({}), BASE_URL, state))
    assert state == {}


def test_cancellation_before_request_preserves_state():
    fake = FakeJenkins({"api": {"builds": [_build(1)]}})
    state = {}

    def cancelled():
        raise RuntimeError("cancelled")

    with pytest.raises(RuntimeError, match="cancelled"):
        list(sync_jenkins(fake, BASE_URL, state, check_active=cancelled))
    assert fake.calls == []
    assert state == {}


def test_http_failures_hide_url_headers_and_close_response():
    from cognee_community_connector_jenkins.jenkins import _get_json

    class Broken:
        def get(self, *args, **kwargs):
            raise RuntimeError("https://user:fixture-secret@example.test Authorization: fixture")

    with pytest.raises(RuntimeError) as error:
        _get_json(Broken(), "https://example.test", "name")
    assert "fixture" not in str(error.value)
    assert error.value.__suppress_context__

    class BadResponse(_Resp):
        def raise_for_status(self):
            raise RuntimeError("Authorization: fixture-secret")

    response = BadResponse(status=500)

    class ReturnedResponse:
        def get(self, *args, **kwargs):
            return response

    with pytest.raises(RuntimeError) as error:
        _get_json(ReturnedResponse(), "https://example.test", "name")
    assert response.closed
    assert "fixture-secret" not in str(error.value)


def test_stats_count_changed_skipped_deleted_and_rate_limit():
    fake = FakeJenkins({"api": {"builds": [_build(1), _build(2, building=True)]}})
    stats = {}
    state = {}
    list(sync_jenkins(fake, BASE_URL, state, stats=stats))
    assert stats == {
        "scanned": 3,
        "skipped": 1,
        "failed": 0,
        "deleted": 0,
        "failed_rate_limit": 0,
        "emitted": 2,
    }
    fake.jobs["api"]["builds"] = []
    rows = list(
        sync_jenkins(fake, BASE_URL, state, stats=stats, confirm_deleted=lambda name, n: n == 1)
    )
    assert _deleted(rows) == ["build:api#1"]
    assert stats == {
        "scanned": 1,
        "skipped": 1,
        "failed": 0,
        "deleted": 1,
        "failed_rate_limit": 0,
        "emitted": 0,
    }

    class Limited(FakeJenkins):
        def get(self, *args, **kwargs):
            return _Resp(status=429)

    with pytest.raises(RuntimeError):
        list(sync_jenkins(Limited({}), BASE_URL, {}, stats=stats))
    assert stats["failed"] == 1
    assert stats["failed_rate_limit"] == 1


def test_old_core_rejected_before_network_or_dlt_state(monkeypatch):
    from cognee_community_connector_jenkins import jenkins as module

    monkeypatch.setattr(module.dlt_utils, "DOCUMENT_SYNC_VERSION", 0, raising=False)
    fake = FakeJenkins({})
    with pytest.raises(RuntimeError, match="document sync"):
        jenkins_source(base_url=BASE_URL, service=fake)
    assert fake.calls == []


def test_retry_after_is_capped_for_seconds_dates_and_clones():
    from email.utils import formatdate
    from time import time

    from cognee_community_connector_jenkins.jenkins import _make_session

    session = _make_session("fixture-user", "fixture-token")
    try:
        retry = session.get_adapter("https://").max_retries
        from types import SimpleNamespace

        response = SimpleNamespace(headers={"Retry-After": "999999"})
        assert retry.get_retry_after(response) == 30
        response.headers = {"Retry-After": formatdate(time() + 86400, usegmt=True)}
        assert retry.new().get_retry_after(response) == 30
        response.headers = {"Retry-After": "2"}
        assert retry.get_retry_after(response) == 2
    finally:
        session.close()


def test_service_alias_and_stats_are_available_on_resource():
    fake = FakeJenkins({"api": {"builds": [_build(1)]}})
    # A previous real DLT test may leave its pipeline active. This source must
    # have its own scope rather than inherit that pipeline's Jenkins cursor.
    resource = jenkins_source(base_url=BASE_URL, service=fake, resource_name="service_alias_stats")
    rows = list(resource)
    assert len(rows) == 2
    assert resource.cognee_sync_stats["emitted"] == 2
    assert resource.cognee_sync_stats["failed"] == 0
    with pytest.raises(ValueError, match="only one"):
        jenkins_source(base_url=BASE_URL, session=fake, service=fake)


def test_cancellation_after_yield_preserves_state():
    import copy

    fake = FakeJenkins({"api": {"builds": [_build(1)]}})
    state = {}
    before = copy.deepcopy(state)
    cancelled = False

    def active():
        if cancelled:
            raise RuntimeError("cancelled")

    iterator = sync_jenkins(fake, BASE_URL, state, check_active=active)
    assert next(iterator)["id"] == "job:api"
    calls_before = len(fake.calls)
    cancelled = True
    with pytest.raises(RuntimeError, match="cancelled"):
        list(iterator)
    assert state == before
    assert len(fake.calls) == calls_before


def test_capped_build_page_preserves_previously_ingested_history():
    class Capped(FakeJenkins):
        def get(self, url, **kwargs):
            response = super().get(url, **kwargs)
            if url.endswith("/job/api/api/json"):
                response._payload["allBuilds"] = response._payload["allBuilds"][:1]
            return response

    state = {}
    fake = FakeJenkins({"api": {"builds": [_build(1), _build(2)]}})
    list(sync_jenkins(fake, BASE_URL, state))
    rows = list(sync_jenkins(Capped(fake.jobs), BASE_URL, state))
    assert _deleted(rows) == []
    assert state["jobs"]["api"]["builds"] == [1, 2]


def test_url_errors_never_include_input_credentials_or_query():
    for url in (
        "ftp://fixture-secret@example.test",
        "https://example.test?token=fixture-secret",
        "https://user:fixture-secret@[invalid",
    ):
        with pytest.raises(ValueError) as error:
            _validate_base_url(url)
        assert "fixture-secret" not in str(error.value)


def test_unbounded_or_unstable_build_pages_abort_without_state_commit():
    import copy

    class Unbounded(FakeJenkins):
        def get(self, url, **kwargs):
            response = super().get(url, **kwargs)
            if url.endswith("/job/api/api/json"):
                response._payload["allBuilds"] = [{"number": n} for n in range(200, 99, -1)]
            return response

    state = {"jobs": {"api": {"last_build": 1, "builds": [1]}}}
    before = copy.deepcopy(state)
    with pytest.raises(RuntimeError, match="unbounded"):
        list(sync_jenkins(Unbounded({"api": {"builds": [_build(2)]}}), BASE_URL, state))
    assert state == before

    class RepeatedPage(FakeJenkins):
        def get(self, url, **kwargs):
            response = super().get(url, **kwargs)
            if url.endswith("/job/api/api/json"):
                response._payload["allBuilds"] = [{"number": 2}]
            return response

    with pytest.raises(RuntimeError, match="unstable"):
        list(sync_jenkins(RepeatedPage({"api": {"builds": [_build(2)]}}), BASE_URL, state))
    assert state == before


def test_partial_build_signal_aborts_before_even_confirmed_deletes():
    import copy

    class Partial(FakeJenkins):
        def get(self, url, **kwargs):
            response = super().get(url, **kwargs)
            if url.endswith("/job/api/api/json"):
                response._payload["truncated"] = True
            return response

    state = {"jobs": {"api": {"last_build": 1, "builds": [1]}}}
    before = copy.deepcopy(state)
    with pytest.raises(RuntimeError, match="incomplete"):
        list(
            sync_jenkins(
                Partial({"api": {"builds": [_build(2)]}}),
                BASE_URL,
                state,
                confirm_deleted=lambda name, n: True,
            )
        )
    assert state == before


def test_failed_dlt_extraction_preserves_persisted_cursor_and_retries(tmp_path):
    import copy

    dlt = pytest.importorskip("dlt")

    def open_pipeline():
        return dlt.pipeline(
            pipeline_name="jenkins_failed_extract",
            destination=dlt.destinations.sqlalchemy(
                f"sqlite:///{(tmp_path / 'failed.db').as_posix()}"
            ),
            dataset_name="ci",
            pipelines_dir=str(tmp_path / "state"),
        )

    fake = FakeJenkins({"api": {"description": "Before", "builds": [_build(1)]}})
    pipeline = open_pipeline()
    pipeline.run(jenkins_source(base_url=BASE_URL, service=fake))
    before_state = copy.deepcopy(pipeline.state.get("sources", {}))
    before_ids = _read_ids(pipeline)
    fake.jobs["api"]["description"] = "After"
    fake.jobs["api"]["builds"].append(_build(2))
    fake.calls.clear()

    def cancel_after_fetch():
        if fake.urls("/2/api/json"):
            raise RuntimeError("fixture cancellation after partial extraction")

    from dlt.pipeline.exceptions import PipelineStepFailed

    with pytest.raises(PipelineStepFailed):
        pipeline.run(
            jenkins_source(base_url=BASE_URL, service=fake, check_active=cancel_after_fetch)
        )
    assert _read_ids(pipeline) == before_ids
    assert pipeline.state.get("sources", {}) == before_state

    # Reopen the same pipeline to check persisted state, rather than just the
    # generator's in-memory dictionary. The failed build must be fetched again.
    fake.calls.clear()
    restored = open_pipeline()
    assert restored.state.get("sources", {}) == before_state
    restored.run(jenkins_source(base_url=BASE_URL, service=fake))
    assert _read_ids(restored) == ["build:api#1", "build:api#2", "job:api"]
    assert fake.urls("/2/api/json")


def test_memory_worker_rejects_shared_remote_and_symlink_paths(tmp_path):
    from remember_worker import within

    root = tmp_path / "sandbox"
    root.mkdir()
    assert within(root, root / "database") == root / "database"
    for path in (tmp_path / "shared.db", "relative.db", "s3://fixture/shared", ""):
        with pytest.raises(AssertionError):
            within(root, path)

    # Windows may forbid creating symlinks; the containment assertion still
    # exercises an escaping existing ancestor on runners supporting symlinks.
    link = root / "escape"
    try:
        link.symlink_to(tmp_path, target_is_directory=True)
    except OSError:
        return
    with pytest.raises(AssertionError):
        within(root, link / "shared.db")


def test_memory_worker_environment_does_not_inherit_credentials(tmp_path, monkeypatch):
    from remember_worker import sandbox_environment

    monkeypatch.setenv("DB_PASSWORD", "fixture-shared-db-secret")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "fixture-cloud-secret")
    monkeypatch.setenv("LLM_API_KEY", "fixture-paid-model-secret")
    env = sandbox_environment(tmp_path)
    assert "DB_PASSWORD" not in env
    assert "AWS_SECRET_ACCESS_KEY" not in env
    assert env["LLM_API_KEY"] == "fixture-unused-model-key"
    assert "fixture-paid-model-secret" not in str(env)
    assert env["DB_PATH"] == str(tmp_path / "relational")


@pytest.mark.parametrize("console", [False, True])
def test_http_cleanup_errors_never_expose_request_details(console):
    from cognee_community_connector_jenkins.jenkins import _get_json, _read_console_tail

    class BrokenClose(_Resp):
        def close(self):
            raise RuntimeError("https://user:fixture-secret@example.test Authorization: fixture")

    class Client:
        def get(self, *args, **kwargs):
            return BrokenClose(payload={"name": "fixture"})

    with pytest.raises(RuntimeError) as error:
        if console:
            _read_console_tail(Client(), BASE_URL, 100)
        else:
            _get_json(Client(), BASE_URL, "name")
    assert "fixture-secret" not in str(error.value)
    assert "Authorization" not in str(error.value)
    assert error.value.__suppress_context__
