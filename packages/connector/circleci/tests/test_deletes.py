"""Forget-on-delete: a project dropped from project_slugs is hard-deleted; a 404 is not."""

import copy
from datetime import datetime

import pytest

from cognee_community_connector_circleci.circleci import (
    DEFAULT_BASE_URL,
    CircleCIAuthError,
    CircleCINotFoundError,
    CircleCIProjectNotFoundError,
    sync_pipelines,
)

SLUG = "gh/rokadepiyush49-rgb/cognee-circleci-fixture"
LISTING = f"/project/{SLUG}/pipeline"
MISSING = "gh/rokadepiyush49-rgb/does-not-exist"  # recorded 404 in errors/
OTHER = "gh/acme/empty"

HOLD = "b7918e16-820b-40e1-9ae0-717c119ce676"
FIRST_FIVE = sorted(
    [
        HOLD,
        "f71b0043-6b6e-4cef-8aca-b96b822bfa0d",
        "08931e5d-1eea-4bca-be5f-6d0ed9ebac99",
        "1b70b7ee-4034-4f6c-924c-0f16bf30d03b",
        "2ddad363-2d2e-4476-9cd1-aed100049957",
    ]
)
TOMBSTONES = [{"id": pipeline_id, "_deleted": True} for pipeline_id in FIRST_FIVE]

NOW = datetime.fromisoformat("2026-10-07T22:00:00+00:00")


def _sync(session, state, slugs=(SLUG,), **kwargs):
    return list(
        sync_pipelines(
            session, DEFAULT_BASE_URL, state, project_slugs=list(slugs), now=NOW, **kwargs
        )
    )


@pytest.fixture
def synced_state(session_for):
    """State after a first sync of the fixture project (5 pipelines known)."""
    state = {}
    _sync(session_for("index.json"), state)
    return state


def test_project_that_404s_fails_and_keeps_its_pipelines(session_for, synced_state):
    # CircleCI answers 404 for a project the token can no longer see, too: a
    # config problem must not wipe the project's history from memory.
    session = session_for("index.json")
    session.queue(LISTING, (404, {"message": "Project not found"}))
    before = copy.deepcopy(synced_state)

    with pytest.raises(CircleCIProjectNotFoundError) as excinfo:
        _sync(session, synced_state)

    assert synced_state == before
    assert excinfo.value.project_slug == SLUG
    assert SLUG not in str(excinfo.value)
    # Nothing else was requested: no re-check of the pending pipeline.
    assert [path for path, _ in session.calls] == [LISTING]


def test_unknown_project_fails(fake_session):
    with pytest.raises(CircleCIProjectNotFoundError):
        _sync(fake_session, {}, slugs=[MISSING])


def test_slug_dropped_from_config_is_forgotten(session_for, synced_state):
    session = session_for("index.json")
    session.queue(f"/project/{OTHER}/pipeline", (200, {"items": [], "next_page_token": None}))
    stats = {}

    rows = _sync(session, synced_state, slugs=[OTHER], stats=stats)

    assert rows == TOMBSTONES
    assert SLUG not in synced_state["projects"]
    assert stats["deleted"] == len(TOMBSTONES)


@pytest.mark.parametrize("status", [401, 403])
def test_auth_errors_fail_without_deleting(session_for, synced_state, status):
    session = session_for("index.json")
    session.queue(LISTING, (status, {"message": "Invalid token provided."}))
    before = copy.deepcopy(synced_state)

    with pytest.raises(CircleCIAuthError):
        _sync(session, synced_state)

    assert synced_state == before


def test_404_for_one_pipeline_is_not_a_missing_project(fake_session):
    main_workflows = "/pipeline/2ddad363-2d2e-4476-9cd1-aed100049957/workflow"
    fake_session.queue(main_workflows, (404, {"message": "Not found"}))

    with pytest.raises(CircleCINotFoundError) as excinfo:
        _sync(fake_session, {})
    assert not isinstance(excinfo.value, CircleCIProjectNotFoundError)


def test_pipelines_that_expire_are_kept(session_for, synced_state, load_fixture):
    # Retention removed every pipeline but the newest from the listing.
    listing = load_fixture("pipelines.json")
    listing["items"] = [p for p in listing["items"] if p["id"] == HOLD]
    session = session_for("index.json")
    session.queue(LISTING, (200, listing))

    assert _sync(session, synced_state) == []
    assert synced_state["projects"][SLUG]["known_ids"] == FIRST_FIVE
