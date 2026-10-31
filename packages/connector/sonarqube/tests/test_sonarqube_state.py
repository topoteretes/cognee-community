"""Checkpoint 2: hash gate, tombstones, mass-delete guards, project handling."""

import json

import pytest
from fake_sonarqube import FakeResponse, FakeSonarQubeSession, issue

from cognee_community_connector_sonarqube.sonarqube import _Client, _Config, _iter_rows


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
# Hash gate
# ---------------------------------------------------------------------------
def test_first_run_emits_everything_second_emits_nothing():
    session = FakeSonarQubeSession(
        projects={"proj": {"name": "Proj", "issues": [issue("A"), issue("B")]}}
    )
    state = {}
    first = _run(session, _config(), state)
    second = _run(session, _config(), state)
    assert len(first) == 2
    assert second == []


def test_changed_message_reemits_only_that_document():
    session = FakeSonarQubeSession(projects={"proj": {"name": "Proj", "issues": [issue("A")]}})
    state = {}
    _run(session, _config(), state)
    session.projects["proj"]["issues"] = [issue("A", message="A new message.")]
    docs = _run(session, _config(), state)
    assert len(docs) == 1
    assert "A new message." in docs[0]["content"]


def test_changed_severity_reemits_the_document():
    session = FakeSonarQubeSession(
        projects={"proj": {"name": "Proj", "issues": [issue("A", severity="MAJOR")]}}
    )
    state = {}
    _run(session, _config(), state)
    session.projects["proj"]["issues"] = [issue("A", severity="CRITICAL")]
    docs = _run(session, _config(), state)
    assert len(docs) == 1
    assert "Severity: CRITICAL" in docs[0]["content"]


def test_resolved_issue_is_tombstoned():
    session = FakeSonarQubeSession(projects={"proj": {"name": "Proj", "issues": [issue("A")]}})
    state = {}
    _run(session, _config(), state)
    session.projects["proj"]["issues"] = [issue("A", resolution="FIXED")]
    docs = _run(session, _config(), state)
    assert len(docs) == 1
    assert docs[0]["_deleted"] is True
    assert docs[0]["id"].endswith(":issue:A")


def test_state_round_trips_through_json():
    session = FakeSonarQubeSession(projects={"proj": {"name": "Proj", "issues": [issue("A")]}})
    state = {}
    _run(session, _config(), state)
    round_tripped = json.loads(json.dumps(state))
    assert round_tripped == state
    # And is usable as the starting state for a later run.
    docs = _run(session, _config(), round_tripped)
    assert docs == []


def test_exception_leaves_state_byte_identical():
    session = FakeSonarQubeSession(projects={"proj": {"name": "Proj", "issues": [issue("A")]}})
    state = {}
    _run(session, _config(), state)
    before = json.dumps(state, sort_keys=True)

    session.script("/api/issues/search", 403, {"errors": [{"msg": "no browse"}]})
    with pytest.raises(RuntimeError):
        _run(session, _config(), state)
    assert json.dumps(state, sort_keys=True) == before


# ---------------------------------------------------------------------------
# Mass-delete guards + spot-check
# ---------------------------------------------------------------------------
def test_all_fixed_project_passes_without_spot_check():
    """Every issue legitimately fixed: guard allows it through once confirmed."""
    issues = [issue(f"I-{n}") for n in range(6)]
    session = FakeSonarQubeSession(projects={"proj": {"name": "Proj", "issues": issues}})
    state = {}
    _run(session, _config(), state)
    session.projects["proj"]["issues"] = [issue(f"I-{n}", resolution="FIXED") for n in range(6)]
    stats = {}
    docs = _run(session, _config(), state, stats=stats)
    assert len(docs) == 6
    assert all(d["_deleted"] for d in docs)
    assert stats["spot_checks"] > 0


def test_guard_raises_when_spot_check_still_matches():
    """A vanished issue that still matches filters means the listing is unreliable."""
    issues = [issue(f"I-{n}") for n in range(6)]
    session = FakeSonarQubeSession(projects={"proj": {"name": "Proj", "issues": issues}})
    state = {}
    _run(session, _config(), state)
    before = json.dumps(state, sort_keys=True)

    # The listing now (falsely) omits everything, but a direct lookup by key
    # still shows the issues matching the current filters -- e.g. a lost
    # permission on the listing endpoint, not a real bulk resolution.
    session.projects["proj"]["issues"] = []

    def fake_issues_search(params):
        if params.get("issues"):
            wanted = set(params["issues"].split(","))
            matches = [i for i in issues if i["key"] in wanted]
            return FakeResponse(200, {"issues": matches, "rules": [], "paging": {"total": 0}})
        return FakeResponse(200, {"issues": [], "rules": [], "paging": {"total": 0}})

    session._issues_search = fake_issues_search
    with pytest.raises(RuntimeError, match="still matches"):
        _run(session, _config(), state)
    assert json.dumps(state, sort_keys=True) == before


def test_guard_bypassed_with_allow_mass_delete():
    issues = [issue(f"I-{n}") for n in range(6)]
    session = FakeSonarQubeSession(projects={"proj": {"name": "Proj", "issues": issues}})
    state = {}
    _run(session, _config(), state)
    session.projects["proj"]["issues"] = []
    docs = _run(session, _config(allow_mass_delete=True), state)
    assert len(docs) == 6
    assert all(d["_deleted"] for d in docs)


def test_empty_desired_with_prior_docs_triggers_guard():
    session = FakeSonarQubeSession(projects={"proj": {"name": "Proj", "issues": [issue("A")]}})
    state = {}
    _run(session, _config(), state)
    # Below the ratio/count thresholds in absolute terms, but it is *all* of
    # the prior documents -- the empty-desired trigger catches this.
    session.projects["proj"]["issues"] = []
    docs = _run(session, _config(), state)
    assert len(docs) == 1
    assert docs[0]["_deleted"] is True


def test_small_change_below_guard_thresholds_needs_no_spot_check():
    issues = [issue(f"I-{n}") for n in range(10)]
    session = FakeSonarQubeSession(projects={"proj": {"name": "Proj", "issues": issues}})
    state = {}
    _run(session, _config(), state)
    session.projects["proj"]["issues"] = [issue(f"I-{n}") for n in range(1, 10)]
    stats = {}
    docs = _run(session, _config(), state, stats=stats)
    assert len(docs) == 1
    assert docs[0]["_deleted"] is True
    assert stats["spot_checks"] == 0


# ---------------------------------------------------------------------------
# Project handling
# ---------------------------------------------------------------------------
def test_explicit_deselect_forgets_without_a_request():
    session = FakeSonarQubeSession(
        projects={
            "a": {"name": "A", "issues": [issue("A-1")]},
            "b": {"name": "B", "issues": [issue("B-1")]},
        }
    )
    state = {}
    _run(session, _config(project_keys=("a", "b")), state)
    session.calls.clear()
    docs = _run(session, _config(project_keys=("a",)), state)
    tombstones = [d for d in docs if d["_deleted"]]
    assert len(tombstones) == 1
    assert tombstones[0]["id"].endswith("B-1")
    assert not any(
        path == "/api/issues/search" and params.get("components") == "b"
        for path, params in session.calls
    )


def test_all_projects_missing_project_is_confirmed_gone():
    session = FakeSonarQubeSession(
        projects={"a": {"name": "A", "issues": [issue("A-1")]}, "b": {"name": "B", "issues": []}}
    )
    state = {}
    _run(session, _config(project_keys=None, all_projects=True), state)
    del session.projects["b"]
    docs = _run(session, _config(project_keys=None, all_projects=True), state)
    assert docs == []  # b had no documents; a is unchanged


def test_all_projects_missing_but_still_exists_raises_inconsistent():
    session = FakeSonarQubeSession(
        projects={"a": {"name": "A", "issues": []}, "b": {"name": "B", "issues": []}}
    )
    state = {}
    _run(session, _config(project_keys=None, all_projects=True), state)
    # b still exists (components/show succeeds) but vanished from the listing.
    session._components_search = lambda params: FakeResponse(
        200, {"components": [{"key": "a", "name": "A"}], "paging": {"total": 1}}
    )
    with pytest.raises(RuntimeError, match="inconsistent"):
        _run(session, _config(project_keys=None, all_projects=True), state)


def test_explicit_never_synced_bad_key_raises():
    session = FakeSonarQubeSession(projects={})
    with pytest.raises(RuntimeError, match="never previously synced"):
        _run(session, _config(project_keys=("ghost",)), {})
