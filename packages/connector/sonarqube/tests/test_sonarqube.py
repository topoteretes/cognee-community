"""Unit tests for the SonarQube dlt connector.

The SonarQube Web API is fully mocked via ``FakeSonar`` — no network, no
credentials, so these run in CI. Coverage:

  - issues, hotspots, and quality-gate outcomes are rendered to documents
    with deep links back to the SonarQube web UI
  - the incremental cursor is ``createdAfter`` on the issues search
  - the keys-only unresolved sweep drives resolution/deletion tombstones
  - hotspots marked REVIEWED (or removed) are tombstoned
  - projects removed from the config (or vanished from the listing) tombstone
    their documents; a project whose fetch fails is skipped without
    mass-deleting, and keeps its cursor state
  - min_severity filters low-severity issues
  - the dlt resource is wired with merge + id PK + the hard_delete column and
    declares the document-source marker
  - a real dlt merge removes the marked row (end-to-end forget-on-delete) and
    persists the incremental state across pipeline runs

The end-to-end "deletion removes it from memory" guarantee is provided by the
existing ``orphan_cleanup`` path in cognee core; here we prove the connector
emits the markers that drive it, and that dlt acts on them.
"""

import pytest

from cognee_community_connector_sonarqube.sonarqube import (
    SONARQUBE_SOURCE_NAME,
    sonarqube_source,
    sync_projects,
)

BASE_URL = "https://sonar.example.com"
PROJECT = "org_repo"
OTHER_PROJECT = "org_other"


def _issue(key, created, message="Bug found", severity="MAJOR", **extra):
    issue = {
        "key": key,
        "rule": "python:S1234",
        "severity": severity,
        "type": "BUG",
        "status": "OPEN",
        "component": f"{PROJECT}:src/main.py",
        "project": PROJECT,
        "message": message,
        "creationDate": created,
        "tags": ["bug"],
    }
    issue.update(extra)
    return issue


def _hotspot(key, message="Weak crypto", status="TO_REVIEW"):
    return {
        "key": key,
        "ruleKey": "python:S5678",
        "message": message,
        "component": f"{PROJECT}:src/crypto.py",
        "project": PROJECT,
        "vulnerabilityProbability": "HIGH",
        "status": status,
    }


def _project_config(**overrides):
    """A fake SonarQube server with one project: gate, issues, hotspots."""
    config = {
        "project": {
            "key": PROJECT,
            "name": "Org Repo",
            "qualifier": "TRK",
            "lastAnalysisDate": "2026-10-01T00:00:00+0000",
        },
        "gate_status": {
            "status": "ERROR",
            "conditions": [
                {"metric": "new_bug_issues", "status": "ERROR", "value": "3", "errorThreshold": "0"}
            ],
        },
        # (key, creationDate, extra-fields) for every unresolved issue.
        "issues": [
            _issue("i1", "2026-09-01T10:00:00+0000"),
            _issue("i2", "2026-09-02T11:00:00+0000", message="Leak", severity="CRITICAL"),
        ],
        "hotspots": [_hotspot("h1")],
    }
    config.update(overrides)
    return config


class _Resp:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        if isinstance(self._payload, Exception):
            raise self._payload

    def json(self):
        return self._payload


class FakeSonar:
    """Minimal stand-in for a ``requests`` session hitting the SonarQube Web API."""

    def __init__(self, projects):
        # projects: {project_key: project_config}
        self.projects = projects

    def get(self, url, params=None):
        params = params or {}
        path = url.split("/api", 1)[1]

        if path == "/projects/search":
            return _Resp(
                {
                    "paging": {"total": len(self.projects)},
                    "components": [p["project"] for p in self.projects.values()],
                }
            )

        if path == "/qualitygates/project_status":
            project = self.projects.get(params.get("projectKey"))
            if project is None:
                return _Resp(ValueError("project not found"))
            gate = project.get("gate_status")
            if gate is None:
                return _Resp(ValueError("no gate configured"))
            return _Resp({"projectStatus": gate})

        if path == "/issues/search":
            project = self.projects.get(params.get("componentKeys"))
            if project is None:
                return _Resp(ValueError("project not found"))
            if params.get("fields") == "key":  # keys-only unresolved sweep
                return _Resp(
                    {
                        "paging": {"total": len(project["issues"])},
                        "issues": [{"key": i["key"]} for i in project["issues"]],
                    }
                )
            created_after = params.get("createdAfter")
            issues = project["issues"]
            if created_after:
                issues = [i for i in issues if i["creationDate"] > created_after]
            if params.get("severities"):
                allowed = set(params["severities"].split(","))
                issues = [i for i in issues if i.get("severity") in allowed]
            return _Resp({"paging": {"total": len(issues)}, "issues": issues})

        if path == "/hotspots/search":
            project = self.projects.get(params.get("project"))
            if project is None:
                return _Resp(ValueError("project not found"))
            return _Resp(
                {"paging": {"total": len(project["hotspots"])}, "hotspots": project["hotspots"]}
            )

        return _Resp(ValueError(f"unexpected URL: {url}"))


def _ids(rows):
    return [row["id"] for row in rows]


def _run(projects, state, **kwargs):
    return list(sync_projects(FakeSonar(projects), BASE_URL, state, **kwargs))


# ---------------------------------------------------------------------------
# Backfill
# ---------------------------------------------------------------------------
def test_backfill_emits_gate_issues_and_hotspots():
    state = {}
    rows = _run({PROJECT: _project_config()}, state)

    assert _ids(rows) == [PROJECT, "i1", "i2", "h1"]
    assert all(row["_deleted"] is False for row in rows)

    gate_row = rows[0]
    assert gate_row["title"] == "Org Repo"
    assert "Quality gate: ERROR" in gate_row["content"]
    assert "new_bug_issues" in gate_row["content"]
    assert gate_row["url"] == f"{BASE_URL}/dashboard?id={PROJECT}"

    issue_row = rows[1]
    assert issue_row["title"] == "Bug found"
    assert "Severity: MAJOR" in issue_row["content"]
    assert "Component: org_repo:src/main.py" in issue_row["content"]
    assert issue_row["url"].startswith(f"{BASE_URL}/project/issues?id={PROJECT}")

    hotspot_row = rows[3]
    assert "Vulnerability probability: HIGH" in hotspot_row["content"]

    # State recorded: cursor per project, hashes per document.
    assert state["projects"][PROJECT]["last_created"] == "2026-09-02T11:00:00+0000"
    assert set(state["issues"]) == {"i1", "i2"}
    assert set(state["hotspots"]) == {"h1"}
    assert state["issues"]["i1"]["project"] == PROJECT


def test_min_severity_filters_low_severity_issues():
    state = {}
    rows = _run({PROJECT: _project_config()}, state, min_severity="CRITICAL")

    # Only the CRITICAL issue passes the threshold; gate + hotspot still sync.
    assert _ids(rows) == [PROJECT, "i2", "h1"]


def test_min_severity_validates_value():
    state = {}
    with pytest.raises(ValueError, match="min_severity"):
        _run({PROJECT: _project_config()}, state, min_severity="HUGE")


# ---------------------------------------------------------------------------
# Incremental (createdAfter cursor)
# ---------------------------------------------------------------------------
def test_incremental_emits_only_new_issues():
    state = {}
    _run({PROJECT: _project_config()}, state)

    newer = _project_config(
        issues=[
            _issue("i1", "2026-09-01T10:00:00+0000"),
            _issue("i2", "2026-09-02T11:00:00+0000", message="Leak", severity="CRITICAL"),
            _issue("i3", "2026-09-05T09:00:00+0000", message="New bug"),
        ]
    )
    rows = _run({PROJECT: newer}, state)

    assert _ids(rows) == ["i3"]
    assert state["projects"][PROJECT]["last_created"] == "2026-09-05T09:00:00+0000"


def test_incremental_no_changes_is_a_noop():
    state = {}
    _run({PROJECT: _project_config()}, state)
    rows = _run({PROJECT: _project_config()}, state)
    assert rows == []


# ---------------------------------------------------------------------------
# Forget-on-delete
# ---------------------------------------------------------------------------
def test_resolved_issue_is_tombstoned_by_sweep():
    state = {}
    _run({PROJECT: _project_config()}, state)

    resolved = _project_config(
        issues=[_issue("i2", "2026-09-02T11:00:00+0000", message="Leak", severity="CRITICAL")]
    )
    rows = _run({PROJECT: resolved}, state)

    assert rows == [{"id": "i1", "_deleted": True}]
    assert "i1" not in state["issues"]


def test_reviewed_hotspot_is_tombstoned():
    state = {}
    _run({PROJECT: _project_config()}, state)

    reviewed = _project_config(hotspots=[_hotspot("h1", status="REVIEWED")])
    rows = _run({PROJECT: reviewed}, state)

    assert rows == [{"id": "h1", "_deleted": True}]
    assert "h1" not in state["hotspots"]


def test_vanished_hotspot_is_tombstoned():
    state = {}
    _run({PROJECT: _project_config()}, state)

    gone = _project_config(hotspots=[])
    rows = _run({PROJECT: gone}, state)

    assert rows == [{"id": "h1", "_deleted": True}]


def test_project_removed_from_config_tombstones_its_documents():
    other = _project_config(
        project={
            "key": OTHER_PROJECT,
            "name": "Other",
            "qualifier": "TRK",
            "lastAnalysisDate": None,
        },
        issues=[_issue("o1", "2026-09-01T10:00:00+0000")],
        hotspots=[],
    )
    other["issues"][0]["project"] = OTHER_PROJECT
    state = {}
    _run({PROJECT: _project_config(), OTHER_PROJECT: other}, state)

    # OTHER_PROJECT dropped from the configuration: its documents are forgotten.
    rows = _run({PROJECT: _project_config()}, state, project_keys=[PROJECT])

    assert {"id": "o1", "_deleted": True} in rows
    assert {"id": OTHER_PROJECT, "_deleted": True} in rows  # quality-gate document
    assert set(state["issues"]) == {"i1", "i2"}


def test_project_vanished_from_listing_tombstones_its_documents():
    state = {}
    _run({PROJECT: _project_config()}, state)

    # The token no longer sees PROJECT at all (deleted upstream / perms revoked).
    rows = _run({}, state)

    assert {"id": "i1", "_deleted": True} in rows
    assert {"id": "i2", "_deleted": True} in rows
    assert {"id": "h1", "_deleted": True} in rows
    assert {"id": PROJECT, "_deleted": True} in rows
    assert state["issues"] == {}


# ---------------------------------------------------------------------------
# Failure posture
# ---------------------------------------------------------------------------
def test_project_fetch_failure_skips_without_deletions_and_keeps_cursor():
    state = {}
    _run({PROJECT: _project_config()}, state)
    cursor = state["projects"][PROJECT]["last_created"]

    broken = _project_config(issues=ValueError("boom"), hotspots=[])
    broken["issues"] = ValueError("boom")
    rows = _run({PROJECT: broken}, state)

    assert rows == []  # skipped, not tombstoned
    assert state["projects"][PROJECT]["last_created"] == cursor  # cursor kept
    assert set(state["issues"]) == {"i1", "i2"}


def test_project_listing_failure_skips_whole_sync():
    state = {}
    _run({PROJECT: _project_config()}, state)

    class BrokenListing(FakeSonar):
        def get(self, url, params=None):
            if "/projects/search" in url:
                return _Resp(ValueError("server down"))
            return super().get(url, params)

    rows = list(sync_projects(BrokenListing({PROJECT: _project_config()}), BASE_URL, state))
    assert rows == []
    assert set(state["issues"]) == {"i1", "i2"}  # state untouched


def test_missing_quality_gate_is_skipped_gracefully():
    state = {}
    no_gate = _project_config(gate_status=None)
    rows = _run({PROJECT: no_gate}, state)

    assert PROJECT not in _ids(rows)  # no gate document
    assert "gate_hash" not in state["projects"][PROJECT]
    assert set(_ids(rows)) == {"i1", "i2", "h1"}


def test_removed_quality_gate_tombstones_its_document():
    state = {}
    _run({PROJECT: _project_config()}, state)

    gate_removed = _project_config(gate_status=None)
    rows = _run({PROJECT: gate_removed}, state)

    assert rows == [{"id": PROJECT, "_deleted": True}]


# ---------------------------------------------------------------------------
# sonarqube_source — dlt wiring — requires dlt
# ---------------------------------------------------------------------------
def test_sonarqube_source_resource_is_configured_for_merge_and_hard_delete():
    pytest.importorskip("dlt")

    resource = sonarqube_source(base_url=BASE_URL, session=FakeSonar({PROJECT: _project_config()}))
    assert resource.name == "sonarqube_documents"

    schema = resource.compute_table_schema()
    write_disposition = schema.get("write_disposition")
    if isinstance(write_disposition, dict):  # dlt may normalize to a config dict
        write_disposition = write_disposition.get("disposition")
    assert write_disposition == "merge"

    columns = schema["columns"]
    assert columns["id"].get("primary_key") is True
    assert columns["_deleted"].get("hard_delete") is True


def test_sonarqube_source_declares_document_marker():
    pytest.importorskip("dlt")
    from cognee.tasks.ingestion.dlt_utils import document_source_tag

    resource = sonarqube_source(base_url=BASE_URL, session=FakeSonar({PROJECT: _project_config()}))
    # resolve_dlt_sources routes on this marker (not the name); keep it stable.
    assert SONARQUBE_SOURCE_NAME == "sonarqube"
    assert document_source_tag(resource) == "sonarqube"


def test_sonarqube_source_requires_token_or_session():
    pytest.importorskip("dlt")
    with pytest.raises(ValueError, match="token"):
        sonarqube_source(base_url=BASE_URL)


def test_sonarqube_source_requires_dlt(monkeypatch):
    import builtins

    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name == "dlt":
            raise ImportError("no dlt")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    with pytest.raises(ImportError, match="cognee-community-connector-sonarqube"):
        sonarqube_source(base_url=BASE_URL, session=FakeSonar({}))


def test_sonarqube_source_validates_base_url():
    pytest.importorskip("dlt")
    with pytest.raises(ValueError, match="base_url"):
        sonarqube_source(base_url="not-a-url", session=FakeSonar({}))


# ---------------------------------------------------------------------------
# End-to-end: a real dlt merge acts on the hard-delete marker, and the
# incremental state persists across pipeline runs
# ---------------------------------------------------------------------------
def test_forget_on_delete_and_incremental_end_to_end_through_a_real_dlt_pipeline(tmp_path):
    dlt = pytest.importorskip("dlt")

    db_path = (tmp_path / "sonar.db").as_posix()
    pipeline = dlt.pipeline(
        pipeline_name="test_sonar_e2e",
        destination=dlt.destinations.sqlalchemy(f"sqlite:///{db_path}"),
        dataset_name="quality",
        pipelines_dir=str(tmp_path / "state"),
    )

    # Sync #1: gate + two issues + one hotspot land in the destination.
    pipeline.run(
        sonarqube_source(base_url=BASE_URL, session=FakeSonar({PROJECT: _project_config()}))
    )
    with pipeline.sql_client() as client:
        assert client.execute_sql("SELECT count(*) FROM sonarqube_documents")[0][0] == 4

    # Sync #2 (same pipeline → persisted dlt state): i1 is resolved upstream
    # and a new issue appears. The connector emits a hard-delete marker plus
    # the new document; the merge applies both.
    later = _project_config(
        issues=[
            _issue("i2", "2026-09-02T11:00:00+0000", message="Leak", severity="CRITICAL"),
            _issue("i3", "2026-09-05T09:00:00+0000", message="New bug"),
        ]
    )
    pipeline.run(sonarqube_source(base_url=BASE_URL, session=FakeSonar({PROJECT: later})))
    with pipeline.sql_client() as client:
        remaining = {row[0] for row in client.execute_sql("SELECT id FROM sonarqube_documents")}

    assert remaining == {PROJECT, "i2", "i3", "h1"}  # i1 forgotten from memory
