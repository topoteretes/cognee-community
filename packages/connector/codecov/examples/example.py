#!/usr/bin/env python3
"""Example demonstration of Codecov coverage-history connector for cognee."""

from cognee_community_connector_codecov.codecov import (
    format_repository_coverage_document,
    format_commit_coverage_document,
    parse_codecov_reports,
    codecov_source,
)

SAMPLE_CODECOV_DATA = {
    "repository": {
        "name": "topoteretes/cognee",
        "default_branch": "main",
        "active": True,
        "totals": {
            "coverage": 91.8,
            "lines": 34500,
            "hits": 31671,
            "misses": 2829,
            "partials": 340,
        },
        "monitored_branches": ["main", "dev", "releases/v1.4"],
    },
    "commits": [
        {
            "commitid": "7b8a9c0d1e2f34567890abcdef1234567890cafe",
            "message": "feat(connectors): add hermetic offline snapshot testing across community modules",
            "author": {"username": "RizulChauhan"},
            "timestamp": "2026-10-24T16:00:00Z",
            "branch": "main",
            "state": "complete",
            "totals": {
                "coverage": 92.4,
                "lines": 34800,
                "hits": 32155,
                "misses": 2645,
            },
            "file_gaps": [
                {
                    "path": "cognee/infrastructure/databases/relational/sqlalchemy.py",
                    "coverage": 78.5,
                    "misses": 54,
                    "notes": "Transaction rollback on deadlocked connection",
                }
            ],
        }
    ],
}


def main():
    print("=" * 70)
    print(" Codecov Coverage-History Connector for Cognee - Demo")
    print("=" * 70)

    # 1. Parse reports
    docs = parse_codecov_reports(
        SAMPLE_CODECOV_DATA,
        service="github",
        owner="topoteretes",
        repo="cognee",
    )
    print(f"\n1. Extracted Coverage Documents: {len(docs)}")
    for doc in docs:
        print(f"   - [{doc['type'].upper()}] ID: {doc['id']} | Node sets: {doc['node_sets']}")

    # 2. Render Repository Summary Preview
    print("\n2. Repository Coverage Summary Preview:")
    print("-" * 50)
    print(format_repository_coverage_document(
        SAMPLE_CODECOV_DATA["repository"],
        service="github",
        owner="topoteretes",
        repo="cognee",
    ))
    print("-" * 50)

    # 3. Render Commit Report Preview
    print("\n3. Commit Coverage Snapshot Preview:")
    print("-" * 50)
    print(format_commit_coverage_document(
        SAMPLE_CODECOV_DATA["commits"][0],
        service="github",
        owner="topoteretes",
        repo="cognee",
    ))
    print("-" * 50)

    # 4. Initialize DLT Source
    print("\n4. Initializing DLT source...")
    try:
        source = codecov_source(snapshot_data=SAMPLE_CODECOV_DATA)
        print("   ✅ DLT source successfully created.")
    except ImportError as e:
        print(f"   ℹ️  dlt extra skipped in minimal environment: {e}")

    print("\n✅ Codecov connector demo completed successfully!")


if __name__ == "__main__":
    main()
