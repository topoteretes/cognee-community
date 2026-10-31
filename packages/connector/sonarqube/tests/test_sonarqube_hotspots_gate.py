"""Checkpoint 3: Security Hotspots and quality-gate documents, with the same
hash gate, tombstone and guard treatment as issues.
"""

import json

import pytest
from fake_sonarqube import FakeResponse, FakeSonarQubeSession, gate, hotspot, issue

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
        "include_issues": False,
        "include_hotspots": True,
        "include_quality_gate": True,
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
# Hotspots: rendering, hash gate, tombstone
# ---------------------------------------------------------------------------
def test_hotspot_rendering_is_deterministic_and_golden():
    session = FakeSonarQubeSession(
        projects={
            "proj": {
                "name": "My Project",
                "hotspots": [
                    hotspot(
                        "HS-1",
                        message="Review this SQL query.",
                        category="sql-injection",
                        probability="HIGH",
                        component="proj:src/db.py",
                        line=55,
                        created="2026-02-01T10:00:00+0000",
                    )
                ],
            }
        }
    )
    state = {}
    docs = _run(session, _config(include_quality_gate=False), state)
    assert len(docs) == 1
    doc = docs[0]
    assert doc["id"] == "sonarqube:sonarqube.example.com:proj:hotspot:HS-1"
    assert doc["title"] == "[SonarQube Hotspot HIGH] Review this SQL query."
    assert doc["url"] == ("https://sonarqube.example.com/security_hotspots?id=proj&hotspots=HS-1")
    assert "Security category: sql-injection" in doc["content"]
    assert "Vulnerability probability: HIGH" in doc["content"]
    assert "File: src/db.py:55" in doc["content"]
    assert "Status: TO_REVIEW" in doc["content"]


def test_only_to_review_hotspots_are_included():
    session = FakeSonarQubeSession(
        projects={
            "proj": {
                "name": "Proj",
                "hotspots": [
                    hotspot("HS-1", status="TO_REVIEW"),
                    hotspot("HS-2", status="REVIEWED", resolution="SAFE"),
                ],
            }
        }
    )
    docs = _run(session, _config(include_quality_gate=False), {})
    assert len(docs) == 1
    assert docs[0]["id"].endswith("HS-1")


def test_hotspot_hash_gate_and_tombstone():
    session = FakeSonarQubeSession(
        projects={"proj": {"name": "Proj", "hotspots": [hotspot("HS-1")]}}
    )
    state = {}
    first = _run(session, _config(include_quality_gate=False), state)
    second = _run(session, _config(include_quality_gate=False), state)
    assert len(first) == 1
    assert second == []

    # Reviewed (resolved): drops out of the TO_REVIEW listing, gets tombstoned.
    session.projects["proj"]["hotspots"] = [hotspot("HS-1", status="REVIEWED", resolution="SAFE")]
    docs = _run(session, _config(include_quality_gate=False), state)
    assert len(docs) == 1
    assert docs[0]["_deleted"] is True


def test_max_hotspots_per_project_raises_instead_of_truncating():
    hotspots = [hotspot(f"HS-{n}") for n in range(5)]
    session = FakeSonarQubeSession(projects={"proj": {"name": "Proj", "hotspots": hotspots}})
    with pytest.raises(RuntimeError, match="max_hotspots_per_project"):
        _run(session, _config(include_quality_gate=False, max_hotspots_per_project=3), {})


# ---------------------------------------------------------------------------
# Quality gate: rendering, hash gate, "NONE" skip
# ---------------------------------------------------------------------------
def test_gate_rendering_golden_failing_conditions():
    session = FakeSonarQubeSession(
        projects={
            "proj": {
                "name": "My Project",
                "gate": gate(
                    status="ERROR",
                    conditions=[
                        {
                            "metricKey": "coverage",
                            "comparator": "LT",
                            "errorThreshold": "80",
                            "actualValue": "42.5",
                            "status": "ERROR",
                        },
                        {
                            "metricKey": "bugs",
                            "comparator": "GT",
                            "errorThreshold": "0",
                            "actualValue": "3",
                            "status": "ERROR",
                        },
                    ],
                ),
            }
        }
    )
    docs = _run(session, _config(include_hotspots=False), {})
    assert len(docs) == 1
    doc = docs[0]
    assert doc["id"] == "sonarqube:sonarqube.example.com:proj:quality-gate"
    assert doc["title"] == "[SonarQube Quality Gate ERROR] My Project"
    assert doc["url"] == "https://sonarqube.example.com/dashboard?id=proj"
    # Sorted by metric key: bugs before coverage.
    bugs_pos = doc["content"].index("bugs:")
    coverage_pos = doc["content"].index("coverage:")
    assert bugs_pos < coverage_pos
    assert "actual 42.5 LT threshold 80" in doc["content"]


def test_gate_none_status_is_skipped():
    session = FakeSonarQubeSession(projects={"proj": {"name": "Proj", "gate": None}})
    docs = _run(session, _config(include_hotspots=False), {})
    assert docs == []


def test_gate_passing_with_no_conditions_still_renders():
    session = FakeSonarQubeSession(
        projects={"proj": {"name": "Proj", "gate": gate(status="OK", conditions=[])}}
    )
    docs = _run(session, _config(include_hotspots=False), {})
    assert len(docs) == 1
    assert "No failing or warning conditions." in docs[0]["content"]


def test_gate_hash_gate_only_reemits_on_change():
    session = FakeSonarQubeSession(
        projects={"proj": {"name": "Proj", "gate": gate(status="ERROR")}}
    )
    state = {}
    first = _run(session, _config(include_hotspots=False), state)
    second = _run(session, _config(include_hotspots=False), state)
    assert len(first) == 1
    assert second == []

    session.projects["proj"]["gate"] = gate(status="OK")
    docs = _run(session, _config(include_hotspots=False), state)
    assert len(docs) == 1
    assert "Gate status: OK" in docs[0]["content"]


def test_gate_disappearing_is_tombstoned_without_spot_check():
    """A project that reverts to NONE: the gate doc vanishes -- no spot-check
    endpoint applies to a single self-confirmed document (see `_check_guards`).
    """
    session = FakeSonarQubeSession(
        projects={"proj": {"name": "Proj", "gate": gate(status="ERROR")}}
    )
    state = {}
    _run(session, _config(include_hotspots=False), state)
    session.projects["proj"]["gate"] = None
    stats = {}
    docs = _run(session, _config(include_hotspots=False), state, stats=stats)
    assert len(docs) == 1
    assert docs[0]["_deleted"] is True
    assert stats["spot_checks"] == 0


# ---------------------------------------------------------------------------
# Hotspot spot-check
# ---------------------------------------------------------------------------
def test_hotspot_guard_spot_check_confirms_reviewed():
    hotspots = [hotspot(f"HS-{n}") for n in range(6)]
    session = FakeSonarQubeSession(projects={"proj": {"name": "Proj", "hotspots": hotspots}})
    state = {}
    _run(session, _config(include_quality_gate=False), state)
    session.projects["proj"]["hotspots"] = [
        hotspot(f"HS-{n}", status="REVIEWED", resolution="FIXED") for n in range(6)
    ]
    stats = {}
    docs = _run(session, _config(include_quality_gate=False), state, stats=stats)
    assert len(docs) == 6
    assert all(d["_deleted"] for d in docs)
    assert stats["spot_checks"] > 0


def test_hotspot_guard_raises_when_spot_check_still_to_review():
    hotspots = [hotspot(f"HS-{n}") for n in range(6)]
    session = FakeSonarQubeSession(projects={"proj": {"name": "Proj", "hotspots": hotspots}})
    state = {}
    _run(session, _config(include_quality_gate=False), state)
    before = json.dumps(state, sort_keys=True)

    session.projects["proj"]["hotspots"] = []

    def fake_hotspots_show(params):
        key = params.get("hotspot")
        match = next((h for h in hotspots if h["key"] == key), None)
        if match is None:
            return FakeResponse(404, {"errors": [{"msg": "not found"}]})
        return FakeResponse(200, dict(match))

    session._hotspots_show = fake_hotspots_show
    with pytest.raises(RuntimeError, match="still matches"):
        _run(session, _config(include_quality_gate=False), state)
    assert json.dumps(state, sort_keys=True) == before


# ---------------------------------------------------------------------------
# All three kinds together
# ---------------------------------------------------------------------------
def test_all_three_kinds_coexist_in_one_project():
    session = FakeSonarQubeSession(
        projects={
            "proj": {
                "name": "Proj",
                "issues": [issue("I-1")],
                "hotspots": [hotspot("HS-1")],
                "gate": gate(status="ERROR"),
            }
        }
    )
    docs = _run(
        session,
        _config(include_issues=True, include_hotspots=True, include_quality_gate=True),
        {},
    )
    kinds = {d["id"].split(":")[-2] for d in docs if "quality-gate" not in d["id"]}
    assert kinds == {"issue", "hotspot"}
    assert any(d["id"].endswith("quality-gate") for d in docs)
    assert len(docs) == 3
