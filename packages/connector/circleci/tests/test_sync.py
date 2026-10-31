"""Sync: cursor, re-checking unfinished and rerun pipelines, first-run limit, per-project state."""

from datetime import datetime, timedelta

import pytest

from cognee_community_connector_circleci.circleci import (
    _MAX_RETRIES,
    DEFAULT_BASE_URL,
    CircleCIRateLimitedError,
    _is_finished,
    sync_pipelines,
)

SLUG = "gh/rokadepiyush49-rgb/cognee-circleci-fixture"
LISTING = f"/project/{SLUG}/pipeline"

# Pipeline ids from the fixtures, newest first.
SLOW_RERUN = "4c2bfb04-d2c8-4449-a78a-ea76aae951c9"  # #6, slow-running/ then slow-finished/
HOLD = "b7918e16-820b-40e1-9ae0-717c119ce676"  # #5, on hold forever
SLOW = "f71b0043-6b6e-4cef-8aca-b96b822bfa0d"  # #4
MANY = "08931e5d-1eea-4bca-be5f-6d0ed9ebac99"  # #3
GREEN = "1b70b7ee-4034-4f6c-924c-0f16bf30d03b"  # #2
MAIN = "2ddad363-2d2e-4476-9cd1-aed100049957"  # #1

# HOLD's workflows: re-checking it lists their jobs.
HOLD_JOBS = {
    "/workflow/c0187f41-f1e6-4687-8941-389330b06b83/job",
    "/workflow/ae05edee-e09e-4321-8b8c-30d563450608/job",
}

HOLD_CREATED = "2026-10-07T21:33:14.855Z"
SLOW_RERUN_CREATED = "2026-10-07T21:48:46.048Z"

# A fixed clock shortly after the fixtures were recorded, so the pending timeout
# and rerun window don't depend on the day the tests run.
NOW = datetime.fromisoformat("2026-10-07T22:00:00+00:00")

# known_ids after the first sync, and after #6 has been seen.
FIRST_FIVE = sorted([HOLD, SLOW, MANY, GREEN, MAIN])
ALL_SIX = sorted([*FIRST_FIVE, SLOW_RERUN])
# Finished pipelines watched for reruns after the first sync.
FINISHED_FOUR = sorted([SLOW, MANY, GREEN, MAIN])


def _sync(session, state, **kwargs):
    kwargs.setdefault("now", NOW)
    return list(sync_pipelines(session, DEFAULT_BASE_URL, state, project_slugs=[SLUG], **kwargs))


def _project(state):
    return state["projects"][SLUG]


def _summary(state):
    """The project's state, with ``recent`` reduced to its sorted pipeline ids."""
    project = dict(_project(state))
    project["recent"] = sorted(project["recent"])
    return project


def _requested(session):
    return [path for path, _ in session.calls]


def _rerun_of(load_fixture, status):
    """MAIN's workflow listing after build-and-test was rerun, with the rerun's jobs."""
    original = load_fixture("main/workflows.json")["items"][0]
    rerun = {**original, "id": "rerun-wf", "status": status, "created_at": "2026-10-07T21:40:00Z"}
    jobs = [{"id": "rerun-job", "name": "test", "status": status, "job_number": 30}]
    return [original, rerun], jobs


def test_first_sync_emits_every_pipeline_and_keeps_unfinished_ones_pending(fake_session):
    state = {}

    rows = _sync(fake_session, state)

    assert [r["id"] for r in rows] == [HOLD, SLOW, MANY, GREEN, MAIN]
    assert _summary(state) == {
        "cursor": HOLD_CREATED,
        "branch": None,
        "pending": [HOLD],
        "recent": FINISHED_FOUR,
        "known_ids": FIRST_FIVE,
    }


def test_second_sync_with_nothing_new_emits_nothing(session_for):
    state = {}
    _sync(session_for("index.json"), state)
    session = session_for("index.json")

    rows = _sync(session, state)

    assert rows == []
    # HOLD is re-checked (still on hold) but not emitted again.
    assert _summary(state) == {
        "cursor": HOLD_CREATED,
        "branch": None,
        "pending": [HOLD],
        "recent": FINISHED_FOUR,
        "known_ids": FIRST_FIVE,
    }
    # Paging stopped at the cursor, and the finished pipelines cost one workflow
    # listing each: only the pending HOLD had its jobs fetched again.
    assert _requested(session).count(LISTING) == 1
    assert {path for path in _requested(session) if path.endswith("/job")} == HOLD_JOBS
    assert f"/pipeline/{SLOW}" not in _requested(session)


def test_new_running_pipeline_is_emitted_and_kept_pending(session_for):
    state = {}
    _sync(session_for("index.json"), state)

    rows = _sync(session_for("index.json", "slow-running/index.json"), state)

    assert [r["id"] for r in rows] == [SLOW_RERUN]
    assert rows[0]["title"].endswith(": running")
    assert _summary(state) == {
        "cursor": SLOW_RERUN_CREATED,
        "branch": None,
        "pending": [HOLD, SLOW_RERUN],
        "recent": FINISHED_FOUR,
        "known_ids": ALL_SIX,
    }


def test_pending_pipeline_is_emitted_again_once_finished(session_for):
    state = {}
    _sync(session_for("index.json"), state)
    _sync(session_for("index.json", "slow-running/index.json"), state)

    rows = _sync(session_for("index.json", "slow-finished/index.json"), state)

    assert [r["id"] for r in rows] == [SLOW_RERUN]
    assert rows[0]["title"].endswith(": failed")
    assert "- slow: success" in rows[0]["content"]
    # Once finished, it is watched for reruns like the others.
    assert _summary(state) == {
        "cursor": SLOW_RERUN_CREATED,
        "branch": None,
        "pending": [HOLD],
        "recent": sorted([*FINISHED_FOUR, SLOW_RERUN]),
        "known_ids": ALL_SIX,
    }


def test_rerun_of_a_finished_pipeline_is_picked_up(session_for, load_fixture):
    state = {}
    _sync(session_for("index.json"), state)
    workflows, jobs = _rerun_of(load_fixture, "success")
    session = session_for("index.json")
    session.queue(f"/pipeline/{MAIN}/workflow", (200, {"items": workflows}))
    session.queue("/workflow/rerun-wf/job", (200, {"items": jobs}))

    rows = _sync(session, state)

    assert [r["id"] for r in rows] == [MAIN]
    # The green rerun decides the status; the failed first run stays as history.
    assert rows[0]["title"] == f"{SLUG} pipeline #1 on main: success"
    assert "Workflow build-and-test: failed" in rows[0]["content"]
    assert "Workflow build-and-test (rerun 1): success" in rows[0]["content"]
    assert "KeyError: 'timeout'" in rows[0]["content"]
    assert "rerun-wf" in _project(state)["recent"][MAIN]["workflows"]


def test_rerun_still_running_is_kept_pending(session_for, load_fixture):
    state = {}
    _sync(session_for("index.json"), state)
    workflows, jobs = _rerun_of(load_fixture, "running")
    session = session_for("index.json")
    session.queue(f"/pipeline/{MAIN}/workflow", (200, {"items": workflows}))
    session.queue("/workflow/rerun-wf/job", (200, {"items": jobs}))

    rows = _sync(session, state)

    assert rows[0]["title"].endswith(": running")
    assert _project(state)["pending"] == [HOLD, MAIN]
    assert MAIN not in _project(state)["recent"]


def test_rerun_window_zero_watches_nothing(fake_session):
    state = {}

    _sync(fake_session, state, rerun_window_days=0)

    assert _project(state)["recent"] == {}


def test_unfinished_pipeline_is_dropped_after_the_timeout(session_for):
    state = {}
    _sync(session_for("index.json"), state)
    session = session_for("index.json")

    rows = _sync(session, state, now=NOW + timedelta(days=8))

    # No longer re-checked; the row emitted on the first sync stays in memory.
    assert rows == []
    assert _project(state)["pending"] == []
    # Past the rerun window too: finished pipelines are dropped without a request.
    assert _project(state)["recent"] == {}
    assert f"/pipeline/{MAIN}/workflow" not in _requested(session)


def test_pending_pipeline_that_is_gone_is_dropped(fake_session):
    state = {"projects": {SLUG: {"cursor": HOLD_CREATED, "pending": ["gone"]}}}
    fake_session.queue("/pipeline/gone", (404, {"message": "Pipeline not found"}))

    assert _sync(fake_session, state) == []
    assert _project(state)["pending"] == []


def test_first_sync_takes_only_the_most_recent_pipelines(fake_session):
    state = {}

    rows = _sync(fake_session, state, max_initial_pipelines=2)

    assert [r["id"] for r in rows] == [HOLD, SLOW]
    assert _project(state)["cursor"] == HOLD_CREATED
    assert f"/pipeline/{MAIN}/workflow" not in _requested(fake_session)


def test_branch_filter_is_sent_to_the_listing(fake_session):
    _sync(fake_session, {}, branch="main")

    listing = [params for path, params in fake_session.calls if path == LISTING]
    assert listing == [{"branch": "main"}]


def test_changing_the_branch_filter_resets_the_cursor(session_for):
    state = {}
    _sync(session_for("index.json"), state, branch="main", rerun_window_days=0)

    same = _sync(session_for("index.json"), state, branch="main", rerun_window_days=0)
    # From main only to all branches: the main cursor would skip every other
    # branch's pipelines, so the listing is read from the start again.
    switched = _sync(session_for("index.json"), state, rerun_window_days=0)

    assert same == []
    # HOLD was already re-checked as a pending pipeline, so it isn't emitted twice.
    assert [r["id"] for r in switched] == [SLOW, MANY, GREEN, MAIN]
    assert _project(state)["branch"] is None
    assert _project(state)["cursor"] == HOLD_CREATED


def test_each_project_keeps_its_own_state(fake_session):
    other = "gh/acme/empty"
    fake_session.queue(f"/project/{other}/pipeline", (200, {"items": [], "next_page_token": None}))
    state = {}

    list(
        sync_pipelines(fake_session, DEFAULT_BASE_URL, state, project_slugs=[SLUG, other], now=NOW)
    )

    assert _summary(state)["known_ids"] == FIRST_FIVE
    assert state["projects"][other] == {
        "cursor": None,
        "branch": None,
        "pending": [],
        "recent": {},
        "known_ids": [],
    }


def test_stats_count_scanned_and_skipped_pipelines(session_for):
    first, second = {}, {}
    state = {}

    _sync(session_for("index.json"), state, stats=first)
    _sync(session_for("index.json"), state, stats=second)

    assert first == {"scanned": 5, "skipped": 0, "failed": 0, "deleted": 0, "failed_rate_limit": 0}
    # HOLD re-checked and still on hold, four finished pipelines without a rerun.
    assert second == {"scanned": 5, "skipped": 5, "failed": 0, "deleted": 0, "failed_rate_limit": 0}


def test_stats_record_a_rate_limited_run(fake_session, no_sleep):
    fake_session.queue(LISTING, *[(429, {})] * _MAX_RETRIES)
    stats = {}

    with pytest.raises(CircleCIRateLimitedError):
        _sync(fake_session, {}, stats=stats)

    assert stats["failed"] == stats["failed_rate_limit"] == 1
    assert all(type(value) is int for value in stats.values())


@pytest.mark.parametrize(
    ("pipeline", "workflows", "expected"),
    [
        ({"state": "created"}, [{"status": "success"}, {"status": "failed"}], True),
        ({"state": "created"}, [{"status": "failed"}, {"status": "running"}], False),
        ({"state": "created"}, [{"status": "on_hold"}], False),
        ({"state": "created"}, [{"status": "failing"}], False),
        # An unknown status is treated as unfinished: re-checked, never wrongly final.
        ({"state": "created"}, [{"status": "some_new_status"}], False),
        ({"state": "errored"}, [], True),
        ({"state": "created"}, [], True),
        ({"state": "setup"}, [], False),
    ],
)
def test_is_finished(pipeline, workflows, expected):
    assert _is_finished(pipeline, workflows) is expected
