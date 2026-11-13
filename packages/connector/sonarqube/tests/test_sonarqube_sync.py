"""Checkpoint 1: issue listing, rendering and the emit-all-each-run sync path."""

from fake_sonarqube import FakeSonarQubeSession, issue

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


def _run(session, config, state=None, stats=None):
    resolved_state = state if state is not None else {}
    resolved_stats = stats if stats is not None else {}
    return list(_iter_rows(_client(session), config, resolved_state, resolved_stats))


def test_emits_one_document_per_issue():
    session = FakeSonarQubeSession(
        projects={"proj": {"name": "Proj", "issues": [issue("ISSUE-1"), issue("ISSUE-2")]}}
    )
    docs = _run(session, _config())
    ids = {d["id"] for d in docs}
    assert ids == {
        "sonarqube:sonarqube.example.com:proj:issue:ISSUE-1",
        "sonarqube:sonarqube.example.com:proj:issue:ISSUE-2",
    }
    assert all(d["_deleted"] is False for d in docs)


def test_unchanged_second_run_emits_nothing():
    session = FakeSonarQubeSession(
        projects={"proj": {"name": "Proj", "issues": [issue("ISSUE-1")]}}
    )
    state = {}
    first = _run(session, _config(), state=state)
    second = _run(session, _config(), state=state)
    assert len(first) == 1
    assert second == []


def test_resolved_issues_are_excluded_by_default():
    session = FakeSonarQubeSession(
        projects={
            "proj": {
                "name": "Proj",
                "issues": [issue("OPEN-1"), issue("FIXED-1", resolution="FIXED")],
            }
        }
    )
    docs = _run(session, _config())
    assert len(docs) == 1
    assert docs[0]["id"].endswith("OPEN-1")


def test_rendering_is_deterministic_and_golden():
    session = FakeSonarQubeSession(
        projects={
            "proj": {
                "name": "My Project",
                "issues": [
                    issue(
                        "ABCD-1",
                        message="Refactor this function.",
                        severity="CRITICAL",
                        type_="BUG",
                        component="proj:src/app.py",
                        line=42,
                        rule="python:S1234",
                        tags=["bug", "security"],
                        created="2026-02-01T10:00:00+0000",
                    )
                ],
            }
        }
    )
    docs = _run(session, _config())
    assert len(docs) == 1
    doc = docs[0]
    assert doc["title"] == "[SonarQube CRITICAL BUG] Refactor this function."
    assert doc["url"] == "https://sonarqube.example.com/project/issues?id=proj&open=ABCD-1"
    assert "Project: My Project (proj)" in doc["content"]
    assert "Rule: python:S1234 (Rule name for python:S1234)" in doc["content"]
    assert "Severity: CRITICAL" in doc["content"]
    assert "Type: BUG" in doc["content"]
    assert "File: src/app.py:42" in doc["content"]
    assert "Tags: bug, security" in doc["content"]
    assert "Created: 2026-02-01T10:00:00+0000" in doc["content"]
    assert "Refactor this function." in doc["content"]
    lineage = "Issue ABCD-1 (rule python:S1234) in src/app.py:42 of project My Project."
    assert lineage in doc["content"]


def test_author_excluded_unless_include_author():
    session = FakeSonarQubeSession(
        projects={"proj": {"name": "Proj", "issues": [issue("ISSUE-1", author="dev@example.com")]}}
    )
    docs = _run(session, _config(include_author=False))
    assert "dev@example.com" not in docs[0]["content"]

    docs = _run(session, _config(include_author=True))
    assert "Author: dev@example.com" in docs[0]["content"]


def test_shuffled_input_renders_identically():
    issues_a = [issue("A", tags=["x", "y"]), issue("B", tags=["y", "x"])]
    issues_b = [issue("B", tags=["x", "y"]), issue("A", tags=["y", "x"])]
    session_a = FakeSonarQubeSession(projects={"proj": {"name": "Proj", "issues": issues_a}})
    session_b = FakeSonarQubeSession(projects={"proj": {"name": "Proj", "issues": issues_b}})
    docs_a = sorted(_run(session_a, _config()), key=lambda d: d["id"])
    docs_b = sorted(_run(session_b, _config()), key=lambda d: d["id"])
    assert docs_a == docs_b


def test_request_includes_filters_branch_and_organization():
    session = FakeSonarQubeSession(projects={"proj": {"name": "Proj", "issues": []}})
    _run(
        session,
        _config(
            branch="main",
            organization=None,
            severities=("CRITICAL", "BLOCKER"),
            issue_types=("BUG",),
            created_after="2026-01-01",
        ),
    )
    _, params = next(p for p in session.calls if p[0] == "/api/issues/search")
    assert params["components"] == "proj"
    assert params["resolved"] == "false"
    assert params["severities"] == "CRITICAL,BLOCKER"
    assert params["types"] == "BUG"
    assert params["branch"] == "main"
    assert params["createdAfter"] == "2026-01-01"


def test_organization_param_sent_when_set():
    session = FakeSonarQubeSession(projects={"proj": {"name": "Proj", "issues": []}})
    _run(session, _config(organization="my-org"))
    _, params = next(p for p in session.calls if p[0] == "/api/issues/search")
    assert params["organization"] == "my-org"


def test_max_issues_per_project_raises_instead_of_truncating():
    issues = [issue(f"I-{n}") for n in range(5)]
    session = FakeSonarQubeSession(projects={"proj": {"name": "Proj", "issues": issues}})
    try:
        _run(session, _config(max_issues_per_project=3))
        raise AssertionError("expected RuntimeError")
    except RuntimeError as exc:
        assert "proj" in str(exc)
        assert "max_issues_per_project" in str(exc)


def test_missing_issue_key_is_skipped_and_counted():
    malformed = {"message": "no key", "severity": "MAJOR", "type": "BUG"}
    session = FakeSonarQubeSession(
        projects={"proj": {"name": "Proj", "issues": [issue("GOOD"), malformed]}}
    )
    stats = {}
    docs = _run(session, _config(), stats=stats)
    assert len(docs) == 1
    assert stats["missing_fields"] == 1


def test_all_projects_lists_every_project():
    session = FakeSonarQubeSession(
        projects={
            "a": {"name": "A", "issues": [issue("A-1")]},
            "b": {"name": "B", "issues": [issue("B-1")]},
        }
    )
    docs = _run(session, _config(project_keys=None, all_projects=True))
    ids = {d["id"] for d in docs}
    assert ids == {
        "sonarqube:sonarqube.example.com:a:issue:A-1",
        "sonarqube:sonarqube.example.com:b:issue:B-1",
    }
