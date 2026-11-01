import pytest

from cognee_community_connector_github_actions.github_actions import (
    GHA_SOURCE_NAME,
    github_actions_source,
    iter_workflow_runs,
)

SAMPLE_RUNS_RESPONSE = {
    "total_count": 1,
    "workflow_runs": [
        {
            "id": 987654,
            "name": "CI Test Suite",
            "head_branch": "main",
            "head_sha": "a1b2c3d4e5f6",
            "event": "push",
            "status": "completed",
            "conclusion": "failure",
            "run_number": 42,
            "created_at": "2024-04-10T12:00:00Z",
            "updated_at": "2024-04-10T12:05:00Z",
            "html_url": "https://github.com/org/repo/actions/runs/987654",
            "actor": {"login": "octocat"},
        }
    ],
}

SAMPLE_JOBS_RESPONSE = {
    "total_count": 1,
    "jobs": [
        {
            "id": 111,
            "name": "unit-tests (python 3.12)",
            "status": "completed",
            "conclusion": "failure",
            "steps": [
                {"name": "Set up Python", "conclusion": "success"},
                {"name": "Run pytest", "conclusion": "failure"},
            ],
        }
    ],
}


def test_iter_workflow_runs_with_diagnostics():
    def mock_fetch(url, params=None):
        if "/jobs" in url:
            return SAMPLE_JOBS_RESPONSE
        return SAMPLE_RUNS_RESPONSE

    runs = list(iter_workflow_runs("org/repo", fetch_fn=mock_fetch))
    assert len(runs) == 1
    r = runs[0]
    assert r["run_id"] == 987654
    assert r["conclusion"] == "failure"
    assert r["actor"] == "octocat"
    assert "Failed Step in unit-tests (python 3.12): `Run pytest` (failure)" in r["content"]
    assert "Conclusion:** FAILURE" in r["content"]


def test_github_actions_source_dlt(tmp_path):
    dlt = pytest.importorskip("dlt")

    def mock_fetch(url, params=None):
        if "/jobs" in url:
            return SAMPLE_JOBS_RESPONSE
        return SAMPLE_RUNS_RESPONSE

    db_path = tmp_path / "gha.db"
    pipeline = dlt.pipeline(
        pipeline_name="gha_test",
        destination=dlt.destinations.sqlalchemy(f"sqlite:///{db_path}"),
        dataset_name="gha_ds",
        pipelines_dir=str(tmp_path / "state"),
    )

    source = github_actions_source("org/repo", fetch_fn=mock_fetch)
    assert getattr(source, "cognee_document_source", None) == GHA_SOURCE_NAME

    pipeline.run(source)
    with pipeline.sql_client() as client:
        rows = client.execute_sql("SELECT run_id, conclusion, repository FROM github_actions_runs")
        assert len(rows) == 1
        assert rows[0][0] == 987654
        assert rows[0][1] == "failure"
        assert rows[0][2] == "org/repo"
