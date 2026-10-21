"""Pipeline → document row, built from recorded API responses."""

from cognee_community_connector_circleci.circleci import (
    DEFAULT_BASE_URL,
    _fetch_workflows,
    _message_tail,
    _overall_status,
    _pipeline_to_row,
)

SLUG = "gh/rokadepiyush49-rgb/cognee-circleci-fixture"


def _row(session, load_fixture, fixture_dir, **caps):
    pipeline = load_fixture(f"{fixture_dir}/pipeline.json")
    workflows = _fetch_workflows(session, DEFAULT_BASE_URL, SLUG, pipeline["id"])
    return _pipeline_to_row(pipeline, workflows, **caps)


def _tests_requested(session):
    return [path for path, _ in session.calls if path.endswith("/tests")]


def test_failed_pipeline_row(fake_session, load_fixture):
    row = _row(fake_session, load_fixture, "main")
    content = row["content"]

    assert row["id"] == "2ddad363-2d2e-4476-9cd1-aed100049957"
    assert row["title"] == f"{SLUG} pipeline #1 on main: failed"
    assert row["url"] == (
        "https://app.circleci.com/pipelines/github/rokadepiyush49-rgb/cognee-circleci-fixture/1"
    )
    assert row["_deleted"] is False
    assert "Branch: main" in content
    assert "Commit: f154c09 Update README for renamed repo" in content
    assert "Workflow build-and-test: failed" in content
    assert "- test: failed\n  Failing tests (3):" in content
    assert "  - tests/test_broken.py::test_parse_config_missing_key" in content
    assert "E       KeyError: 'timeout'" in content
    assert "- smoke: failed (no test results)" in content
    assert "- lint: success" in content
    assert "- deploy: not_run" in content


def test_tests_are_fetched_for_failed_jobs_only(fake_session, load_fixture):
    _row(fake_session, load_fixture, "main")

    # test (job 1) and smoke (job 3) failed; lint and deploy did not.
    assert _tests_requested(fake_session) == [
        f"/project/{SLUG}/1/tests",
        f"/project/{SLUG}/3/tests",
    ]


def test_passing_pipeline_is_status_lines_only(fake_session, load_fixture):
    row = _row(fake_session, load_fixture, "green")

    assert row["title"].endswith(": success")
    assert "Failing tests" not in row["content"]
    assert _tests_requested(fake_session) == []


def test_failing_tests_are_capped(fake_session, load_fixture):
    row = _row(fake_session, load_fixture, "many-failures", max_failing_tests=5)

    assert "Failing tests (28, showing 5):" in row["content"]
    assert row["content"].count("\n  - tests/") == 5


def test_long_messages_keep_their_end(fake_session, load_fixture):
    content = _row(fake_session, load_fixture, "main", max_message_chars=200)["content"]

    # The error and its location survive; the test's source at the start does not.
    assert "tests/test_broken.py:33: AssertionError" in content
    assert "def test_long_failure_message" not in content
    assert "E       KeyError: 'timeout'" in content


def test_message_tail_starts_on_a_whole_line():
    message = (
        "def test_x():\n    assert f() == 1\nE   assert 2 == 1\n\ntests/test_x.py:2: AssertionError"
    )

    assert _message_tail(message, 1000) == message
    assert _message_tail(message, 40) == "...\n\ntests/test_x.py:2: AssertionError"
    assert _message_tail("x" * 100, 10) == "..." + "x" * 10


def test_unfinished_pipelines(fake_session, session_for, load_fixture):
    running = _row(session_for("slow-running/index.json"), load_fixture, "slow-running")
    finished = _row(session_for("slow-finished/index.json"), load_fixture, "slow-finished")
    on_hold = _row(fake_session, load_fixture, "hold")

    assert running["title"].endswith(": running")
    assert "- slow: running" in running["content"]
    # Same pipeline, now final: build-and-test failed, so the pipeline failed.
    assert finished["id"] == running["id"]
    assert finished["title"].endswith(": failed")
    assert "- slow: success" in finished["content"]
    assert on_hold["title"].endswith(": on_hold")
    assert "- deploy: blocked" in on_hold["content"]


def test_pipeline_without_workflows_shows_its_state_and_errors():
    pipeline = {
        "id": "p1",
        "number": 7,
        "project_slug": SLUG,
        "state": "errored",
        "created_at": "2026-10-08T10:00:00Z",
        "errors": [{"type": "config", "message": "Config does not conform to schema"}],
        "vcs": {"branch": "main"},
        "trigger": {"type": "webhook"},
    }

    row = _pipeline_to_row(pipeline, [])

    assert row["title"] == f"{SLUG} pipeline #7 on main: errored"
    assert "Error (config): Config does not conform to schema" in row["content"]


def test_overall_status_prefers_unfinished_then_worst():
    assert _overall_status({}, [{"status": "failed"}, {"status": "running"}]) == "running"
    assert _overall_status({}, [{"status": "success"}, {"status": "failed"}]) == "failed"
    assert _overall_status({}, [{"status": "success"}]) == "success"
