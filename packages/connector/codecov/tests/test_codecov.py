"""Tests for Codecov coverage-history connector for cognee."""

import pytest
from pathlib import Path
from cognee_community_connector_codecov.codecov import (
    DOCUMENT_SOURCE_ATTR,
    format_repository_coverage_document,
    format_commit_coverage_document,
    parse_codecov_reports,
    codecov_source,
)


@pytest.fixture
def mock_codecov_payload():
    return {
        "repository": {
            "name": "apex-automations/trading-swarm",
            "default_branch": "main",
            "active": True,
            "totals": {
                "coverage": 94.5,
                "lines": 12500,
                "hits": 11812,
                "misses": 688,
                "partials": 120,
            },
            "monitored_branches": ["main", "staging", "v2-release"],
        },
        "commits": [
            {
                "commitid": "a1b2c3d4e5f67890123456789abcdef012345678",
                "message": "feat(engine): add defensive KEV risk scalar regularizer",
                "author": {"username": "RizulChauhan"},
                "timestamp": "2026-10-24T14:30:00Z",
                "branch": "main",
                "state": "complete",
                "totals": {
                    "coverage": 95.2,
                    "lines": 12600,
                    "hits": 11995,
                    "misses": 605,
                },
                "file_gaps": [
                    {
                        "path": "trading_engine/order_executor.py",
                        "coverage": 72.4,
                        "misses": 42,
                        "notes": "Edge cases in slippage limit rejection",
                    }
                ],
            },
            {
                # Pending report - coverage is None
                "commitid": "f9e8d7c6b5a432109876543210fedcba09876543",
                "message": "fix(network): handle Base RPC 429 backoff",
                "author": {"username": "RizulChauhan"},
                "timestamp": "2026-10-24T15:00:00Z",
                "branch": "main",
                "state": "pending",
                "totals": {
                    "coverage": None,
                    "lines": 12620,
                    "hits": 0,
                    "misses": 0,
                },
            },
        ],
    }


def test_format_repository_coverage_document(mock_codecov_payload):
    md = format_repository_coverage_document(
        mock_codecov_payload["repository"],
        service="github",
        owner="apex-automations",
        repo="trading-swarm",
    )
    assert "[Codecov Repository Summary] apex-automations/trading-swarm" in md
    assert "EXCELLENT (94.50%)" in md
    assert "12,500" in md
    assert "11,812" in md
    assert "`main`, `staging`, `v2-release`" in md


def test_format_commit_coverage_document(mock_codecov_payload):
    c = mock_codecov_payload["commits"][0]
    md = format_commit_coverage_document(
        c,
        service="github",
        owner="apex-automations",
        repo="trading-swarm",
    )
    assert "[Codecov Commit Report] `a1b2c3d4`" in md
    assert "add defensive KEV risk scalar regularizer" in md
    assert "EXCELLENT (95.20%)" in md
    assert "RizulChauhan" in md
    assert "trading_engine/order_executor.py" in md
    assert "72.4%" in md


def test_pending_report_formatting(mock_codecov_payload):
    c_pending = mock_codecov_payload["commits"][1]
    md = format_commit_coverage_document(
        c_pending,
        service="github",
        owner="apex-automations",
        repo="trading-swarm",
    )
    assert "PENDING / NO REPORT" in md


def test_parse_codecov_reports(mock_codecov_payload):
    docs = parse_codecov_reports(
        mock_codecov_payload,
        service="github",
        owner="apex-automations",
        repo="trading-swarm",
    )
    assert len(docs) == 3  # 1 repo summary + 2 commits
    repo_doc = next(d for d in docs if d["type"] == "repo_summary")
    assert "codecov:repo:github:apex-automations:trading-swarm" in repo_doc["node_sets"]

    commit_docs = [d for d in docs if d["type"] == "commit_report"]
    assert len(commit_docs) == 2
    assert "codecov:commit:a1b2c3d4e5f67890123456789abcdef012345678" in commit_docs[0]["node_sets"]
    assert "codecov:branch:main" in commit_docs[0]["node_sets"]


def test_missing_snapshot_file_error():
    with pytest.raises(FileNotFoundError):
        codecov_source(snapshot_data="/non/existent/codecov_snapshot.json")


def test_codecov_source_with_snapshot(mock_codecov_payload):
    try:
        import dlt
    except ImportError:
        pytest.skip("dlt is not installed in the test environment")

    source = codecov_source(snapshot_data=mock_codecov_payload)
    assert source is not None
