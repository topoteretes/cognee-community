"""Trivy connector demo — "Ask my DevSecOps & CVE Posture".

Ingest Trivy security scans (vulnerabilities, misconfigurations, and secrets)
into Cognee memory, incrementally, with automatic forget-on-delete.

────────────────────────────────────────────────────────────────────────────
One-time setup:
────────────────────────────────────────────────────────────────────────────
1. Install package:
       pip install cognee-community-connector-trivy

2. Generate a Trivy report for any container or repo:
       trivy image -f json -o scan.json python:3.11-slim

3. Set your LLM keys in .env as required by cognee.

Run it:
    python examples/example.py
"""

import asyncio

import cognee

from cognee_community_connector_trivy import trivy_source

DATASET_NAME = "security_posture"
RESOURCE_NAME = "trivy_scan_main"

TRIVY_REMEMBER_KWARGS = {
    "primary_key": "id",
    "write_disposition": "merge",
    "max_rows_per_table": 0,
}


async def sync(sample_data: dict) -> None:
    source = trivy_source(json_data=sample_data, resource_name=RESOURCE_NAME)
    result = await cognee.remember(source, dataset_name=DATASET_NAME, **TRIVY_REMEMBER_KWARGS)
    print("Ingestion result:", result)
    print("Sync stats:", getattr(source, "cognee_sync_stats", {}))


async def main():
    sample_scan = {
        "SchemaVersion": 2,
        "ArtifactName": "backend-service:v1.0.0",
        "ArtifactType": "container_image",
        "Results": [
            {
                "Target": "backend-service:v1.0.0 (alpine 3.18)",
                "Vulnerabilities": [
                    {
                        "VulnerabilityID": "CVE-2023-38545",
                        "PkgName": "curl",
                        "InstalledVersion": "8.1.2-r0",
                        "FixedVersion": "8.4.0-r0",
                        "Severity": "CRITICAL",
                        "Title": "SOCKS5 heap buffer overflow",
                    }
                ],
            }
        ],
    }

    print("\n=== Ingesting Trivy security scan report ===")
    await sync(sample_scan)

    print("\n=== Asking Cognee about vulnerability remediation ===")
    query = (
        "Which packages in backend-service contain critical vulnerabilities "
        "and what are their fixed versions?"
    )
    answer = await cognee.search(
        query_text=query,
        dataset_name=DATASET_NAME,
    )
    print("\nAnswer from Cognee Security Knowledge Graph:")
    print(answer)


if __name__ == "__main__":
    asyncio.run(main())
