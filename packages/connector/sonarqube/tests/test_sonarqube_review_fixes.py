"""Regression tests for bugs found in a pre-commit review.

1. Branch isolation: two configs for the same project on different branches
   must not collide on document id or state key, and must not tombstone
   each other.
2. all_projects: a project beyond the per-run probe cap that's missing from
   the listing must keep its state (not be silently dropped), so it can
   still be tombstoned once it's actually confirmed gone.
3. organization must be sent on every Cloud call, including the hotspot
   spot-check.
4. The issue/hotspot listing must not raise a spurious "crossed the window"
   error when the total is an exact multiple of the page size.
5. auth_scheme="basic" uses HTTP Basic with an empty password.
"""

import json

import pytest
from fake_sonarqube import FakeSonarQubeSession, issue

from cognee_community_connector_sonarqube import sonarqube_source
from cognee_community_connector_sonarqube.sonarqube import (
    _Client,
    _Config,
    _iter_rows,
    _make_session,
)


def _config(**overrides):
    defaults = {
        "base_url": "https://sonarqube.example.com",
        "organization": None,
        "project_keys": ("proj",),
        "all_projects": False,
        "branch": None,
        "severities": ("MAJOR", "CRITICAL", "BLOCKER"),
        "issue_types": ("CODE_SMELL", "BUG", "VULNERABILITY"),
        "include_issues": True,
        "include_hotspots": False,
        "include_quality_gate": False,
        "created_after": None,
        "max_issues_per_project": 5000,
        "max_hotspots_per_project": 1000,
        "include_author": False,
        "timeout": 30.0,
        "allow_mass_delete": False,
    }
    defaults.update(overrides)
    return _Config(**defaults)


def _client(session):
    return _Client(session, "https://sonarqube.example.com", 30.0)


def _run(session, config, state, stats=None):
    return list(_iter_rows(_client(session), config, state, stats if stats is not None else {}))


# ---------------------------------------------------------------------------
# 1. Branch isolation
# ---------------------------------------------------------------------------
def test_different_branches_get_different_document_ids():
    session = FakeSonarQubeSession(
        projects={"proj": {"name": "Proj", "issues": [issue("SAME-KEY")]}}
    )
    main_docs = _run(session, _config(branch="main"), {})
    feature_docs = _run(session, _config(branch="feature-x"), {})
    assert main_docs[0]["id"] != feature_docs[0]["id"]
    assert "branch:main" in main_docs[0]["id"]
    assert "branch:feature-x" in feature_docs[0]["id"]


def test_syncing_one_branch_does_not_touch_the_other_branchs_state():
    session = FakeSonarQubeSession(
        projects={"proj": {"name": "Proj", "issues": [issue("ISSUE-MAIN")]}}
    )
    state = {}
    _run(session, _config(branch="main"), state)

    session.projects["proj"]["issues"] = [issue("ISSUE-FEATURE")]
    docs = _run(session, _config(branch="feature-x"), state)

    # Only the feature-x issue is emitted; main's issue is untouched, not tombstoned.
    assert len(docs) == 1
    assert docs[0]["id"].endswith("ISSUE-FEATURE")
    assert docs[0]["_deleted"] is False

    # A second main-branch run with nothing changed still emits nothing,
    # proving main's state survived the feature-x run intact.
    session.projects["proj"]["issues"] = [issue("ISSUE-MAIN")]
    docs = _run(session, _config(branch="main"), state)
    assert docs == []


def test_both_branches_state_entries_coexist():
    session = FakeSonarQubeSession(projects={"proj": {"name": "Proj", "issues": [issue("A")]}})
    state = {}
    _run(session, _config(branch="main"), state)
    _run(session, _config(branch="develop"), state)
    assert len(state["projects"]) == 2


# ---------------------------------------------------------------------------
# 2. all_projects probe-cap state preservation
# ---------------------------------------------------------------------------
def test_projects_beyond_the_probe_cap_keep_their_state_instead_of_vanishing():
    projects = {f"p{i}": {"name": f"P{i}", "issues": [issue(f"I-{i}")]} for i in range(25)}
    session = FakeSonarQubeSession(projects=projects)
    state = {}
    _run(session, _config(project_keys=None, all_projects=True), state)
    assert len(state["projects"]) == 25

    session.projects = {}  # every project vanishes from the listing at once
    docs = _run(session, _config(project_keys=None, all_projects=True), state)

    # 20 probed and confirmed gone -> tombstoned; 5 beyond the cap are kept,
    # not dropped, so they remain confirmable (and tombstonable) later.
    tombstoned = {d["id"] for d in docs}
    assert len(tombstoned) == 20
    assert len(state["projects"]) == 5

    # Those 5 are eventually confirmed gone too, once probed in a later run.
    docs2 = _run(session, _config(project_keys=None, all_projects=True), state)
    assert len(docs2) == 5
    assert state["projects"] == {}


# ---------------------------------------------------------------------------
# 3. organization on every call, including the hotspot spot-check
# ---------------------------------------------------------------------------
def test_organization_sent_on_hotspot_spot_check():
    from fake_sonarqube import hotspot as make_hotspot

    hotspots = [make_hotspot(f"HS-{n}") for n in range(6)]
    session = FakeSonarQubeSession(
        projects={"proj": {"name": "Proj", "hotspots": hotspots}}, organization="my-org"
    )
    state = {}
    _run(
        session,
        _config(include_issues=False, include_hotspots=True, organization="my-org"),
        state,
    )
    session.projects["proj"]["hotspots"] = [
        make_hotspot(f"HS-{n}", status="REVIEWED", resolution="FIXED") for n in range(6)
    ]
    session.calls.clear()
    _run(
        session,
        _config(include_issues=False, include_hotspots=True, organization="my-org"),
        state,
    )
    show_calls = [params for path, params in session.calls if path == "/api/hotspots/show"]
    assert show_calls, "expected at least one hotspots/show spot-check call"
    assert all(params.get("organization") == "my-org" for params in show_calls)


# ---------------------------------------------------------------------------
# 4. Exact page-size multiple at the window boundary
# ---------------------------------------------------------------------------
def test_total_exactly_at_the_window_limit_does_not_raise(monkeypatch):
    import cognee_community_connector_sonarqube.sonarqube as module

    monkeypatch.setattr(module, "_ISSUES_PAGE_SIZE", 2)
    monkeypatch.setattr(module, "_SEARCH_WINDOW_LIMIT", 4)
    issues = [issue(f"I-{i}") for i in range(4)]
    session = FakeSonarQubeSession(projects={"proj": {"name": "Proj", "issues": issues}})
    client = _client(session)
    result, _rules = module._list_issues(client, _config(max_issues_per_project=10), "proj")
    assert len(result) == 4


def test_total_exactly_at_the_window_limit_for_hotspots_does_not_raise(monkeypatch):
    from fake_sonarqube import hotspot as make_hotspot

    import cognee_community_connector_sonarqube.sonarqube as module

    monkeypatch.setattr(module, "_HOTSPOTS_PAGE_SIZE", 2)
    monkeypatch.setattr(module, "_SEARCH_WINDOW_LIMIT", 4)
    hotspots = [make_hotspot(f"HS-{i}") for i in range(4)]
    session = FakeSonarQubeSession(projects={"proj": {"name": "Proj", "hotspots": hotspots}})
    client = _client(session)
    result = module._list_hotspots(client, _config(max_hotspots_per_project=10), "proj")
    assert len(result) == 4


def test_one_past_the_window_limit_still_raises(monkeypatch):
    """The fix must not disable the guard entirely -- a real overflow still raises."""
    import cognee_community_connector_sonarqube.sonarqube as module

    monkeypatch.setattr(module, "_ISSUES_PAGE_SIZE", 2)
    monkeypatch.setattr(module, "_SEARCH_WINDOW_LIMIT", 4)
    issues = [issue(f"I-{i}") for i in range(6)]
    session = FakeSonarQubeSession(projects={"proj": {"name": "Proj", "issues": issues}})
    client = _client(session)
    with pytest.raises(RuntimeError, match="window"):
        module._list_issues(client, _config(max_issues_per_project=10), "proj")


# ---------------------------------------------------------------------------
# 5. auth_scheme
# ---------------------------------------------------------------------------
def test_make_session_bearer_is_the_default():
    session = _make_session("tok-123")
    assert session.headers.get("Authorization") == "Bearer tok-123"
    assert session.auth is None


def test_make_session_basic_uses_token_as_username_empty_password():
    session = _make_session("tok-123", "basic")
    assert session.auth == ("tok-123", "")
    assert "Authorization" not in session.headers


def test_factory_rejects_invalid_auth_scheme():
    with pytest.raises(ValueError, match="auth_scheme"):
        sonarqube_source(
            token="tok",
            base_url="https://sonarqube.example.com",
            project_keys=["proj"],
            auth_scheme="digest",
            session=FakeSonarQubeSession(projects={"proj": {"name": "Proj"}}),
        )


# ---------------------------------------------------------------------------
# State round-trips through JSON with the new branch field
# ---------------------------------------------------------------------------
def test_state_with_branch_round_trips_through_json():
    session = FakeSonarQubeSession(projects={"proj": {"name": "Proj", "issues": [issue("A")]}})
    state = {}
    _run(session, _config(branch="main"), state)
    round_tripped = json.loads(json.dumps(state))
    assert round_tripped == state
    docs = _run(session, _config(branch="main"), round_tripped)
    assert docs == []
