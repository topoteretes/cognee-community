"""Sync: cursor, re-checking unfinished pipelines, first-run limit, per-project state."""

from datetime import datetime, timedelta

import pytest

from cognee_community_connector_circleci.circleci import (
    DEFAULT_BASE_URL,
    _is_finished,
    sync_pipelines,
)

SLUG = "gh/rokadepiyush49-rgb/cognee-circleci-fixture"

# Pipeline ids from the fixtures, newest first.
SLOW_RERUN = "4c2bfb04-d2c8-4449-a78a-ea76aae951c9"  # #6, slow-running/ then slow-finished/
HOLD = "b7918e16-820b-40e1-9ae0-717c119ce676"  # #5, on hold forever
SLOW = "f71b0043-6b6e-4cef-8aca-b96b822bfa0d"  # #4
MANY = "08931e5d-1eea-4bca-be5f-6d0ed9ebac99"  # #3
GREEN = "1b70b7ee-4034-4f6c-924c-0f16bf30d03b"  # #2
MAIN = "2ddad363-2d2e-4476-9cd1-aed100049957"  # #1

HOLD_CREATED = "2026-10-07T21:33:14.855Z"
SLOW_RERUN_CREATED = "2026-10-07T21:48:46.048Z"

# A fixed clock shortly after the fixtures were recorded, so the pending timeout
# doesn't depend on the day the tests run.
NOW = datetime.fromisoformat("2026-10-07T22:00:00Z")


def _sync(session, state, **kwargs):
    kwargs.setdefault("now", NOW)
    return list(sync_pipelines(session, DEFAULT_BASE_URL, state, project_slugs=[SLUG], **kwargs))


def _project(state):
    return state["projects"][SLUG]


def _requested(session):
    return [path for path, _ in session.calls]


def test_first_sync_emits_every_pipeline_and_keeps_unfinished_ones_pending(fake_session):
    state = {}

    rows = _sync(fake_session, state)

    assert [r["id"] for r in rows] == [HOLD, SLOW, MANY, GREEN, MAIN]
    assert _project(state) == {"cursor": HOLD_CREATED, "pending": [HOLD]}


def test_second_sync_with_nothing_new_emits_nothing(session_for):
    state = {}
    _sync(session_for("index.json"), state)
    session = session_for("index.json")

    rows = _sync(session, state)

    assert rows == []
    # HOLD is re-checked (still on hold) but not emitted again.
    assert _project(state) == {"cursor": HOLD_CREATED, "pending": [HOLD]}
    # Paging stopped at the cursor: no older pipeline was fetched again.
    assert f"/pipeline/{SLOW}/workflow" not in _requested(session)


def test_new_running_pipeline_is_emitted_and_kept_pending(session_for):
    state = {}
    _sync(session_for("index.json"), state)

    rows = _sync(session_for("index.json", "slow-running/index.json"), state)

    assert [r["id"] for r in rows] == [SLOW_RERUN]
    assert rows[0]["title"].endswith(": running")
    assert _project(state) == {"cursor": SLOW_RERUN_CREATED, "pending": [HOLD, SLOW_RERUN]}


def test_pending_pipeline_is_emitted_again_once_finished(session_for):
    state = {}
    _sync(session_for("index.json"), state)
    _sync(session_for("index.json", "slow-running/index.json"), state)

    rows = _sync(session_for("index.json", "slow-finished/index.json"), state)

    assert [r["id"] for r in rows] == [SLOW_RERUN]
    assert rows[0]["title"].endswith(": failed")
    assert "- slow: success" in rows[0]["content"]
    assert _project(state) == {"cursor": SLOW_RERUN_CREATED, "pending": [HOLD]}


def test_unfinished_pipeline_is_dropped_after_the_timeout(session_for):
    state = {}
    _sync(session_for("index.json"), state)

    rows = _sync(session_for("index.json"), state, now=NOW + timedelta(days=8))

    # No longer re-checked; the row emitted on the first sync stays in memory.
    assert rows == []
    assert _project(state)["pending"] == []


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

    listing = [params for path, params in fake_session.calls if path == f"/project/{SLUG}/pipeline"]
    assert listing == [{"branch": "main"}]


def test_each_project_keeps_its_own_state(fake_session):
    other = "gh/acme/empty"
    fake_session.queue(f"/project/{other}/pipeline", (200, {"items": [], "next_page_token": None}))
    state = {}

    list(
        sync_pipelines(fake_session, DEFAULT_BASE_URL, state, project_slugs=[SLUG, other], now=NOW)
    )

    assert state["projects"][SLUG] == {"cursor": HOLD_CREATED, "pending": [HOLD]}
    assert state["projects"][other] == {"cursor": None, "pending": []}


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
