# CISA Known Exploited Vulnerabilities (KEV) Connector for Cognee

The official `cisa_kev` data-source connector for [Cognee](https://github.com/topoteretes/cognee), syncing authoritative cyber vulnerability intelligence from CISA's Known Exploited Vulnerabilities (KEV) catalog directly into AI agent memory and graph/vector stores.

---

## Overview

CISA's Known Exploited Vulnerabilities (KEV) catalog is the authoritative baseline of vulnerabilities confirmed to be actively exploited by threat actors in the wild.

This connector transforms the raw CISA KEV JSON feed into rich, searchable markdown documents tagged for Cognee's document ingestion pipeline (`cognify()`). Security teams, DevSecOps agents, and platform engineers can query, reason over, and synthesize actionable remediation intelligence across their infrastructure.

### Key Capabilities

- **Authoritative Ingestion**: Pulls directly from `https://www.cisa.gov/sites/default/files/feeds/known_exploited_vulnerabilities.json` or local offline snapshots.
- **Strict Data Integrity**: Enforces response validation, declared count verification (`len(vulnerabilities) == declared_count`), and regex verification (`^CVE-\d{4}-\d{4,}$`) to prevent truncated or corrupted feeds from affecting staging.
- **Document-Mode Routing**: Declares `DOCUMENT_SOURCE_ATTR = "cisa_kev"` so records flow through Cognee's entity extraction and relationship graph instead of raw tabular schemas.
- **Clean Reconciliation**: Operates under `write_disposition="replace"`, allowing removals or catalog updates to propagate cleanly via Cognee's `orphan_cleanup`.
- **Targeted Filtering**: Built-in parameters for filtering by `since_date`, `vendor`, `product`, or `known_ransomware_only`.

---

## Installation

```bash
pip install "cognee-community-connector-cisa-kev"
```

Or when developing from the `cognee-community` monorepo:

```bash
cd packages/connector/cisa_kev
pip install -e .
```

---

## Quickstart

### 1. Ingest Full KEV Catalog into Cognee

```python
import asyncio
import cognee
from cognee_community_connector_cisa_kev import cisa_kev_source


async def main():
    # 1. Create the CISA KEV dlt source
    source = cisa_kev_source()

    # 2. Ingest into Cognee staging
    await cognee.add(source)

    # 3. Cognify: extract entities, relationships, and vector embeddings
    await cognee.cognify()

    # 4. Search agent memory
    results = await cognee.search(
        "Which vulnerabilities are actively used in ransomware campaigns and have upcoming due dates?"
    )
    for res in results:
        print(res)


if __name__ == "__main__":
    asyncio.run(main())
```

---

### 2. Filtered Ingestion

You can restrict ingestion to recent additions, specific vendors, or active ransomware threats:

```python
# Ingest only vulnerabilities added in 2026 for a specific vendor
source = cisa_kev_source(
    since_date="2026-01-01",
    vendor="Ivanti",
)

# Ingest only vulnerabilities confirmed in ransomware campaigns
ransomware_source = cisa_kev_source(
    known_ransomware_only=True,
)
```

---

### 3. Air-Gapped / Offline Local File Ingestion

For air-gapped CI/CD environments or reproducible testing:

```python
source = cisa_kev_source(
    feed_url_or_path="/var/cache/cisa/known_exploited_vulnerabilities.json"
)
```

---

## Document Schema & Metadata

Each vulnerability is formatted as a structured Markdown document and tagged with:

| Field | Type | Description |
|---|---|---|
| `id` | `str` | Canonical CVE identifier (e.g. `CVE-2026-118527`) |
| `cve_id` | `str` | CVE identifier |
| `title` | `str` | Standardized header `[CVE-...] <Vulnerability Name>` |
| `vendor_project` | `str` | Vendor or open source project name |
| `product` | `str` | Affected software product or component |
| `vulnerability_name` | `str` | Descriptive title of vulnerability |
| `date_added` | `str` | Date added to the CISA KEV catalog (`YYYY-MM-DD`) |
| `due_date` | `str` | Federal remediation due date (`YYYY-MM-DD`) |
| `known_ransomware_use` | `str` | `Known` or `Unknown` |
| `cwes` | `list[str]` | Associated Common Weakness Enumerations |
| `url` | `str` | NIST National Vulnerability Database URL |
| `catalog_version` | `str` | CISA catalog release version |
| `content` | `str` | Structured Markdown body with remediation directives |

---

## Running Tests

Run the hermetic test suite:

```bash
pytest packages/connector/cisa_kev/tests/ -v
```
