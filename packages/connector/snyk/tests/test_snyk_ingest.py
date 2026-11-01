"""A real ``dlt.pipeline`` run into SQLite, the way cognee stages a document source.

The table that cognee later reads back holds one row per vulnerability. No network and no
LLM: the Snyk side is the in-memory fake.
"""

import json

import dlt
import pytest
from fake_snyk import FakeSnykSession, error_response, make_issue, make_project

from cognee_community_connector_snyk import snyk_source

TOKEN = "snyk-secret-token-0123456789"
LODASH = "snyk:us-01:o1:vuln:SNYK-JS-LODASH-567746"
MINIMIST = "snyk:us-01:o1:vuln:SNYK-JS-MINIMIST-559764"


def fake(issues=None) -> FakeSnykSession:
    session = FakeSnykSession()
    session.add_org(
        "o1",
        "Acme",
        projects=[make_project("p1", "acme/shop"), make_project("p2", "acme/admin")],
        issues=issues
        if issues is not None
        else [
            make_issue(project="p1", issue_id="lodash-p1"),
            make_issue(project="p2", version="4.17.11", issue_id="lodash-p2"),
            make_issue(
                project="p1",
                issue_id="minimist-p1",
                title="Prototype Pollution in minimist",
                snyk_id="SNYK-JS-MINIMIST-559764",
                cves=("CVE-2020-7598",),
                package="minimist",
                version="1.2.0",
            ),
        ],
    )
    return session


def run(tmp_path, session):
    pipeline = dlt.pipeline(
        pipeline_name="test_snyk_ingest",
        pipelines_dir=str(tmp_path / "pipelines"),
        destination=dlt.destinations.sqlalchemy(f"sqlite:///{tmp_path / 'snyk.db'}"),
        dataset_name="snyk",
    )
    source = snyk_source(TOKEN, org_ids=["o1"], session=session)
    pipeline.run(source, write_disposition="merge", primary_key="id")
    return pipeline, source


def rows(pipeline) -> dict[str, str]:
    with pipeline.sql_client() as client:
        result = client.execute_sql("SELECT id, content FROM snyk_vulnerabilities ORDER BY id")
    return {row[0]: row[1] for row in result}


def test_initial_sync_stages_one_row_per_vulnerability(tmp_path):
    pipeline, source = run(tmp_path, fake())
    staged = rows(pipeline)
    assert set(staged) == {LODASH, MINIMIST}
    assert "acme/shop" in staged[LODASH]
    assert "acme/admin" in staged[LODASH]
    assert "minimist@1.2.0" in staged[MINIMIST]
    assert source.cognee_sync_stats["vulnerabilities"] == 2


def test_a_rerun_is_idempotent(tmp_path):
    pipeline, _ = run(tmp_path, fake())
    first = rows(pipeline)
    pipeline, _ = run(tmp_path, fake())
    assert rows(pipeline) == first
    assert len(first) == 2


def test_a_changed_vulnerability_is_updated_in_place(tmp_path):
    pipeline, _ = run(tmp_path, fake())
    first = rows(pipeline)
    session = fake()
    session.issues["o1"].append(make_issue(project="p2", version="4.17.4"))
    pipeline, _ = run(tmp_path, session)
    second = rows(pipeline)
    assert second[LODASH] != first[LODASH]
    assert "lodash@4.17.4" in second[LODASH]
    assert second[MINIMIST] == first[MINIMIST]
    assert len(second) == 2


def test_an_api_failure_propagates_and_leaves_the_table_unchanged(tmp_path):
    pipeline, _ = run(tmp_path, fake())
    before = rows(pipeline)
    session = fake()
    session.script = [RuntimeError("Snyk is down")]
    with pytest.raises(Exception, match="Snyk is down"):
        run(tmp_path, session)
    assert rows(pipeline) == before


def test_a_forbidden_token_fails_the_run_with_the_enterprise_hint(tmp_path):
    session = fake()
    session.script = [error_response(403, "Forbidden")]
    with pytest.raises(Exception, match="Enterprise"):
        run(tmp_path, session)


def find(node, key):
    """First value stored under ``key`` anywhere in the pipeline state."""
    if isinstance(node, dict):
        if key in node:
            return node[key]
        for value in node.values():
            found = find(value, key)
            if found is not None:
                return found
    return None


def test_an_unchanged_rerun_emits_nothing_and_keeps_every_row(tmp_path):
    pipeline, _ = run(tmp_path, fake())
    first = rows(pipeline)
    pipeline, source = run(tmp_path, fake())
    assert rows(pipeline) == first
    assert source.cognee_sync_stats["emitted"] == 0
    assert source.cognee_sync_stats["unchanged"] == 2


def test_the_saved_state_has_hashes_and_no_secrets(tmp_path):
    pipeline, _ = run(tmp_path, fake())
    saved = find(pipeline.state, "orgs")
    assert set(saved["us-01:o1"]["docs"]) == {LODASH, MINIMIST}
    dumped = json.dumps(pipeline.state, default=str)
    assert TOKEN not in dumped
    assert "api.snyk.io" not in dumped


def test_a_forgotten_vulnerability_is_physically_gone_after_the_resync(tmp_path):
    pipeline, _ = run(tmp_path, fake())
    assert set(rows(pipeline)) == {LODASH, MINIMIST}
    session = fake()
    session.issues["o1"] = [
        i for i in session.issues["o1"] if i["attributes"]["title"].endswith("lodash")
    ]
    pipeline, source = run(tmp_path, session)
    assert set(rows(pipeline)) == {LODASH}
    assert source.cognee_sync_stats["deleted"] == 1


def test_a_fixed_finding_re_emits_the_document_without_it(tmp_path):
    pipeline, _ = run(tmp_path, fake())
    session = fake()
    session.issues["o1"] = [
        i for i in session.issues["o1"] if i["relationships"]["scan_item"]["data"]["id"] != "p2"
    ]
    pipeline, _ = run(tmp_path, session)
    staged = rows(pipeline)
    assert "acme/admin" not in staged[LODASH]
    assert "acme/shop" in staged[LODASH]


def test_a_refused_deletion_fails_the_run_and_leaves_the_table_unchanged(tmp_path):
    pipeline, _ = run(tmp_path, fake())
    before = rows(pipeline)
    session = fake()
    session.hide_listing = True  # the listing is empty, the findings are still there
    with pytest.raises(Exception, match="returned no matching findings"):
        run(tmp_path, session)
    assert rows(pipeline) == before
