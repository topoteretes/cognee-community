"""Focused tests for Jenkins listing, build cursors and deletion markers."""

from __future__ import annotations

import asyncio
import importlib.util
from pathlib import Path
from urllib.parse import urlsplit

import pytest

from cognee_community_connector_jenkins.jenkins import (
    _MAX_CONSOLE_BYTES,
    _make_session,
    _sync_jenkins,
    _validate_base_url,
)


class FakeResponse:
    def __init__(self, data=None, body=b"", status_code=200):
        self.data = data or {}
        self.body = body
        self.status_code = status_code
        self.closed = False

    def raise_for_status(self):
        if self.status_code >= 400:
            error = RuntimeError(f"HTTP {self.status_code}")
            error.response = self
            raise error

    def json(self):
        return self.data

    def iter_content(self, chunk_size):
        for start in range(0, len(self.body), chunk_size):
            yield self.body[start : start + chunk_size]

    def close(self):
        self.closed = True


class FakeJenkins:
    def __init__(self, jobs):
        self.jobs = jobs
        self.builds = {}
        self.consoles = {}
        self.calls = []

    def get(self, url, *, params=None, timeout=None, stream=False):
        self.calls.append((url, params or {}, stream))
        path = urlsplit(url).path
        if path == "/api/json":
            return FakeResponse({"jobs": list(self.jobs.values())})

        jobs = list(self.jobs.values())
        while jobs:
            job = jobs.pop()
            jobs.extend(job.get("children", []))
            job_path = urlsplit(job["url"]).path.rstrip("/")
            if path == f"{job_path}/api/json":
                return FakeResponse({"jobs": job.get("children", [])})
            if path.startswith(f"{job_path}/"):
                suffix = path[len(job_path) + 1 :].split("/")
                if suffix[-1] == "consoleText":
                    return FakeResponse(body=self.consoles.get(url, b""))
                if len(suffix) >= 3 and suffix[-2:] == ["api", "json"] and suffix[0].isdigit():
                    number = int(suffix[0])
                    if (job_path, number) in self.builds:
                        return FakeResponse(self.builds[(job_path, number)])
                    return FakeResponse(status_code=404)
        raise AssertionError(f"Unexpected Jenkins URL: {url}")


def _job(
    name,
    *,
    number=None,
    description="job description",
    disabled=False,
    base_url="http://jenkins",
):
    return {
        "name": name.rsplit("/", 1)[-1],
        "fullName": name,
        "url": f"{base_url}/job/{name.replace('/', '/job/')}/",
        "_class": "hudson.model.FreeStyleProject",
        "description": description,
        "disabled": disabled,
        "buildable": not disabled,
        "lastBuild": {"number": number} if number is not None else None,
    }


def _build(session, name, number, result, *, building=False):
    path = urlsplit(_job(name)["url"]).path.rstrip("/")
    build = {
        "number": number,
        "result": result,
        "building": building,
        "timestamp": number * 1000,
        "duration": 250,
        "displayName": f"#{number}",
        "url": f"http://jenkins{path}/{number}/",
    }
    session.builds[(path, number)] = build
    return build


def test_initial_and_incremental_sync_fetch_each_new_build_and_only_failed_logs():
    job = _job("service", number=1)
    session = FakeJenkins({"service": job})
    _build(session, "service", 1, "SUCCESS")
    state = {}

    first_rows, state = _sync_jenkins(
        session,
        "http://jenkins",
        state,
        job_names=None,
        include_job_config=True,
        include_builds=True,
        initial_builds=20,
    )

    assert [row["id"].split("::")[-2:] for row in first_rows if "build" in row["id"]] == [
        ["build", "1"]
    ]
    assert len([call for call in session.calls if call[2]]) == 0
    assert state["instances"]["http://jenkins"]["last_builds"] == {"service": 1}

    job["lastBuild"] = {"number": 3}
    _build(session, "service", 2, "FAILURE")
    _build(session, "service", 3, "UNSTABLE")
    failed_url = "http://jenkins/job/service/2/consoleText"
    session.consoles[failed_url] = b"unit test failed"

    next_rows, next_state = _sync_jenkins(
        session,
        "http://jenkins",
        state,
        job_names=None,
        include_job_config=True,
        include_builds=True,
        initial_builds=20,
    )

    builds = [row for row in next_rows if "::build::" in row["id"]]
    assert [row["id"].rsplit("::", 1)[-1] for row in builds] == ["2", "3"]
    assert "unit test failed" in builds[0]["content"]
    assert "unit test failed" not in builds[1]["content"]
    console_calls = [call[0] for call in session.calls if "consoleText" in call[0]]
    assert console_calls == [failed_url]
    assert not [row for row in next_rows if row["id"].endswith("::job::service")]
    assert next_state["instances"]["http://jenkins"]["last_builds"]["service"] == 3


def test_pending_build_is_revisited_after_a_later_build_finishes():
    job = _job("service", number=1)
    session = FakeJenkins({"service": job})
    _build(session, "service", 1, None, building=True)

    first_rows, state = _sync_jenkins(
        session,
        "http://jenkins",
        {},
        job_names=None,
        include_job_config=False,
        include_builds=True,
        initial_builds=20,
    )
    assert first_rows == []
    assert state["instances"]["http://jenkins"]["pending_builds"] == {"service": [1]}

    job["lastBuild"] = {"number": 2}
    _build(session, "service", 1, "SUCCESS")
    _build(session, "service", 2, "SUCCESS")
    rows, state = _sync_jenkins(
        session,
        "http://jenkins",
        state,
        job_names=None,
        include_job_config=False,
        include_builds=True,
        initial_builds=20,
    )
    assert {row["id"].rsplit("::", 1)[-1] for row in rows} == {"1", "2"}
    assert state["instances"]["http://jenkins"]["pending_builds"]["service"] == []


def test_job_configuration_is_allowlisted_and_changes_emit_a_stable_job_id():
    job = _job("team/service", number=None)
    session = FakeJenkins({"team/service": job})

    first, state = _sync_jenkins(
        session,
        "http://jenkins",
        {},
        job_names=None,
        include_job_config=True,
        include_builds=False,
        initial_builds=20,
    )
    assert len(first) == 1
    row = first[0]
    assert row["id"] == "http://jenkins::job::team/service"
    assert "job description" in row["content"]
    assert "config.xml" not in row["content"]

    unchanged, state = _sync_jenkins(
        session,
        "http://jenkins",
        state,
        job_names=None,
        include_job_config=True,
        include_builds=False,
        initial_builds=20,
    )
    assert unchanged == []

    job["disabled"] = True
    changed, _ = _sync_jenkins(
        session,
        "http://jenkins",
        state,
        job_names=None,
        include_job_config=True,
        include_builds=False,
        initial_builds=20,
    )
    assert changed[0]["id"] == row["id"]
    assert "Disabled: True" in changed[0]["content"]


def test_deleted_job_emits_hard_delete_for_job_and_ingested_builds():
    first_job = _job("service", number=1)
    second_job = _job("other", number=None)
    session = FakeJenkins({"service": first_job, "other": second_job})
    _build(session, "service", 1, "SUCCESS")
    _, state = _sync_jenkins(
        session,
        "http://jenkins",
        {},
        job_names=None,
        include_job_config=True,
        include_builds=True,
        initial_builds=20,
    )

    del session.jobs["service"]
    rows, state = _sync_jenkins(
        session,
        "http://jenkins",
        state,
        job_names=None,
        include_job_config=True,
        include_builds=True,
        initial_builds=20,
    )
    tombstones = {row["id"] for row in rows if row["_deleted"]}
    assert tombstones == {
        "http://jenkins::job::service",
        "http://jenkins::job::service::build::1",
    }
    assert state["instances"]["http://jenkins"]["known_jobs"] == ["other"]


def test_empty_unscoped_inventory_does_not_delete_prior_data():
    session = FakeJenkins({"service": _job("service")})
    _, state = _sync_jenkins(
        session,
        "http://jenkins",
        {},
        job_names=None,
        include_job_config=True,
        include_builds=False,
        initial_builds=20,
    )
    session.jobs.clear()

    rows, next_state = _sync_jenkins(
        session,
        "http://jenkins",
        state,
        job_names=None,
        include_job_config=True,
        include_builds=False,
        initial_builds=20,
    )
    assert rows == []
    assert next_state == state


def test_job_selection_can_reconcile_a_deleted_final_job():
    session = FakeJenkins({"service": _job("service")})
    _, state = _sync_jenkins(
        session,
        "http://jenkins",
        {},
        job_names=["service"],
        include_job_config=True,
        include_builds=False,
        initial_builds=20,
    )
    session.jobs.clear()

    rows, _ = _sync_jenkins(
        session,
        "http://jenkins",
        state,
        job_names=["service"],
        include_job_config=True,
        include_builds=False,
        initial_builds=20,
    )
    assert rows == [{"id": "http://jenkins::job::service", "_deleted": True}]


def test_nested_folder_jobs_and_bounded_api_requests():
    child = _job("team/service")
    folder = {
        "name": "team",
        "fullName": "team",
        "url": "http://jenkins/job/team/",
        "_class": "com.cloudbees.hudson.plugins.folder.Folder",
        "children": [child],
    }
    session = FakeJenkins({"team": folder})
    _build(session, "team/service", 1, "SUCCESS")
    child["lastBuild"] = {"number": 1}

    rows, _ = _sync_jenkins(
        session,
        "http://jenkins",
        {},
        job_names=None,
        include_job_config=True,
        include_builds=True,
        initial_builds=20,
    )
    assert {row["id"] for row in rows} == {
        "http://jenkins::job::team/service",
        "http://jenkins::job::team/service::build::1",
    }
    assert all(call[1].get("depth") == 1 for call in session.calls)
    assert all("tree" in call[1] for call in session.calls)


def test_foreign_job_urls_are_rejected_before_authentication_is_forwarded():
    session = FakeJenkins({"evil": {**_job("evil"), "url": "http://other.example/job/evil/"}})
    with pytest.raises(ValueError, match="outside the configured instance"):
        _sync_jenkins(
            session,
            "http://jenkins",
            {},
            job_names=None,
            include_job_config=True,
            include_builds=False,
            initial_builds=20,
        )
    assert len(session.calls) == 1


def test_failed_console_is_capped():
    from cognee_community_connector_jenkins.jenkins import _get_console_text

    body = b"x" * (_MAX_CONSOLE_BYTES + 10)
    response = FakeResponse(body=body)

    class Session:
        def get(self, url, **kwargs):
            assert kwargs["stream"] is True
            return response

    text = _get_console_text(Session(), "http://jenkins/job/service/1/consoleText")
    assert len(text.encode("utf-8")) < _MAX_CONSOLE_BYTES + 100
    assert "truncated" in text
    assert response.closed


def test_invalid_or_credential_bearing_base_urls_are_rejected():
    for value in ("", "jenkins.example.com", "http://user:pass@jenkins.example.com"):
        with pytest.raises(ValueError):
            _validate_base_url(value)


def test_http_session_uses_basic_auth_without_putting_token_in_headers():
    session = _make_session("jenkins-user", "test-token")
    assert session.auth == ("jenkins-user", "test-token")
    assert "test-token" not in str(session.headers)


def test_instance_cursor_state_is_separate_for_each_jenkins_url():
    first = FakeJenkins({"service": _job("service", base_url="http://jenkins-a")})
    second = FakeJenkins({"service": _job("service", base_url="http://jenkins-b")})
    _, state = _sync_jenkins(
        first,
        "http://jenkins-a",
        {},
        job_names=None,
        include_job_config=True,
        include_builds=False,
        initial_builds=20,
    )
    rows, state = _sync_jenkins(
        second,
        "http://jenkins-b",
        state,
        job_names=None,
        include_job_config=True,
        include_builds=False,
        initial_builds=20,
    )
    assert len(rows) == 1
    assert rows[0]["id"] == "http://jenkins-b::job::service"
    assert set(state["instances"]) == {"http://jenkins-a", "http://jenkins-b"}


def test_dlt_resource_declares_document_mode_and_hard_delete():
    pytest.importorskip("dlt")
    pytest.importorskip("cognee")
    from cognee.tasks.ingestion.dlt_utils import document_source_tag

    from cognee_community_connector_jenkins import jenkins_source

    resource = jenkins_source("http://jenkins", session=FakeJenkins({}), include_builds=False)
    assert document_source_tag(resource) == "jenkins"
    schema = resource.compute_table_schema()
    assert schema["columns"]["id"]["primary_key"] is True
    assert schema["write_disposition"] == "merge"
    assert schema["columns"]["_deleted"]["hard_delete"] is True


def test_dlt_merge_removes_deleted_job_and_build_rows(tmp_path):
    dlt = pytest.importorskip("dlt")
    pytest.importorskip("cognee")
    from cognee_community_connector_jenkins import jenkins_source

    session = FakeJenkins({"service": _job("service", number=1), "other": _job("other")})
    _build(session, "service", 1, "SUCCESS")
    pipeline = dlt.pipeline(
        pipeline_name="jenkins_test",
        destination=dlt.destinations.sqlalchemy(f"sqlite:///{tmp_path / 'jenkins.db'}"),
        dataset_name="jenkins_test",
        pipelines_dir=str(tmp_path / "state"),
    )

    pipeline.run(jenkins_source("http://jenkins", session=session))
    with (
        pipeline.sql_client() as client,
        client.execute_query("SELECT id FROM jenkins_items") as cursor,
    ):
        before = {row[0] for row in cursor.fetchall()}
    assert "http://jenkins::job::service" in before
    assert "http://jenkins::job::service::build::1" in before

    del session.jobs["service"]
    pipeline.run(jenkins_source("http://jenkins", session=session))
    with (
        pipeline.sql_client() as client,
        client.execute_query("SELECT id FROM jenkins_items") as cursor,
    ):
        after = {row[0] for row in cursor.fetchall()}
    assert after == {"http://jenkins::job::other"}


def test_example_without_job_selection_syncs_all_jobs(monkeypatch):
    example_path = Path(__file__).parents[1] / "examples" / "example.py"
    spec = importlib.util.spec_from_file_location("jenkins_example", example_path)
    example = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(example)

    for name, value in {
        "JENKINS_URL": "http://jenkins",
        "JENKINS_USER": "user",
        "JENKINS_API_TOKEN": "token",
    }.items():
        monkeypatch.setenv(name, value)
    monkeypatch.delenv("JENKINS_JOB_NAMES", raising=False)

    captured = {}

    def fake_source(**kwargs):
        captured.update(kwargs)
        return object()

    async def fake_remember(*_args, **_kwargs):
        return None

    async def fake_search(*_args, **_kwargs):
        return "mocked search result"

    monkeypatch.setattr(example, "jenkins_source", fake_source)
    monkeypatch.setattr(example.cognee, "remember", fake_remember)
    monkeypatch.setattr(example.cognee, "search", fake_search)

    asyncio.run(example.main())

    assert captured["job_names"] is None
