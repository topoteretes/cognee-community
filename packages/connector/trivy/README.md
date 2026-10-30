# cognee-community-connector-trivy

A Trivy security vulnerability, misconfiguration, and secret audit connector for [cognee](https://github.com/topoteretes/cognee): turn your container scans, repository audits, and Kubernetes posture into AI memory — *"Ask my DevSecOps & CVE posture"*.

It provides a `dlt` resource feeding directly into `cognee.remember(...)`, integrating seamlessly with Cognee's DLT ingestion path (`DOCUMENT_SOURCE_ATTR` routing → `cognify` entity/relationship extraction → `orphan_cleanup`).

## Features

- **Vulnerability Tracking (CVEs)**: Ingests package vulnerabilities, CVE identifiers, CVSS v3 scores, installed vs. fixed versions, and advisory links across Debian, Alpine, RHEL, npm, PyPI, and Go modules.
- **Infrastructure Misconfigurations**: Tracks Kubernetes, Dockerfile, and Terraform misconfigurations (e.g. root privilege escalation, missing resource limits) with remediation advice.
- **Secret Detection with Zero-Leak Redaction**: Parses leaked secrets (AWS keys, GitHub tokens, private keys) while strictly redacting the sensitive payload so that raw credentials are never persisted into vector stores or AI knowledge graphs.
- **Incremental Sync & Deduplication**: Fingerprints each security finding via deterministic SHA-256 hashes. Unchanged scan results are skipped on re-syncs, saving token and processing budgets.
- **Forget-on-Delete / Patch Reconciliation**: When a vulnerability is patched in a subsequent scan or a misconfiguration is resolved, tombstone records (`_deleted=True`) are emitted on `merge`, triggering Cognee's `orphan_cleanup` to purge resolved CVE nodes from the knowledge graph.
- **Multi-Source Support**: Ingest from single JSON report files, entire directories of scan outputs, Python dictionaries, or live Trivy server endpoints.

## Requirements

- Python 3.10 to 3.14.
- `cognee >= 1.6.2, < 2`
- `dlt[sqlalchemy] >= 1.9.0, < 2`
- `requests >= 2.31.0, < 3`

## Installation

```bash
uv pip install cognee-community-connector-trivy
# or in development:
cd packages/connector/trivy && uv sync
```

## Quick Start

```python
import asyncio
import cognee
from cognee_community_connector_trivy import trivy_source


async def main():
    # Ingest a Trivy JSON scan report
    source = trivy_source(
        report_path="trivy-scan.json",
        resource_name="production_cluster_scans",
    )

    await cognee.remember(
        source,
        dataset_name="security_posture",
        primary_key="id",
        write_disposition="merge",
        max_rows_per_table=0,
    )

    # Query CVE posture with AI
    answer = await cognee.search(
        query_text="Which container images contain critical CVEs affecting OpenSSL or curl with available fixes?",
        dataset_name="security_posture",
    )
    print(answer)


if __name__ == "__main__":
    asyncio.run(main())
```

## Ingested Objects & Markdown Documents

Each security finding is parsed into a structured Markdown document:

| Finding Type | ID Format | Ingested Details |
|---|---|---|
| **Vulnerability** | `trivy:vuln:{artifact}:{target}:{cve}:{pkg}` | CVE ID, Severity, Package name, Installed version, Fixed version, CVSS score, Advisory link, Description |
| **Misconfiguration** | `trivy:misconfig:{artifact}:{target}:{rule_id}` | Rule ID, Check type, Severity, Status (`FAIL`), Recommended remediation |
| **Secret** | `trivy:secret:{artifact}:{target}:{rule}:{line}` | Secret title, Category, File path & lines, Redacted preview (raw credential never exposed) |
| **Security Summary** | `trivy:summary:{artifact}` | Total CVE breakdown (Critical, High, Medium, Low), misconfig count, leaked secret count, OS |

## Testing

Hermetic, offline unit tests using recorded Trivy JSON fixtures:

```bash
pytest
```
