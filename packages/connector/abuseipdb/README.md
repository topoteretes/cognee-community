# AbuseIPDB Threat Intelligence Connector for Cognee

The official `abuseipdb` data-source connector for [Cognee](https://github.com/topoteretes/cognee), syncing external IP reputation, attack categories, and abuse confidence scores directly into AI agent memory and graph/vector stores.

---

## Overview

Security operations (SecOps) and platform engineering teams regularly investigate suspicious external IP addresses from authentication logs, firewall drops, or cloud audit trails.

This connector integrates with the [AbuseIPDB REST API v2](https://docs.abuseipdb.com/) to fetch threat intelligence for suspect IP addresses, automatically resolving integer attack categories (such as SSH brute-force, web app attacks, port scans, and DDoS bots) into human-readable markdown documents for Cognee's document ingestion pipeline (`cognify()`).

### Key Capabilities

- **Automated IP Triage**: Ingests abuse confidence scores (0-100%), ISP, domain, usage type, country, and TOR exit node status.
- **Category Resolution**: Automatically maps AbuseIPDB category IDs (1 to 23: e.g. `18` -> Brute-Force, `22` -> SSH, `21` -> Web App Attack, `14` -> Port Scan) into structured text that Cognee can link to MITRE ATT&CK nodes.
- **Private IP Guard**: Locally validates and filters RFC 1918, loopback, and link-local IP addresses using Python's `ipaddress` module before making external requests, saving API quota.
- **Document-Mode Routing**: Declares `DOCUMENT_SOURCE_ATTR = "abuseipdb"`, routing records through Cognee's entity extraction and relationship graph instead of raw tabular schemas.
- **Reconciliation & Orphan Cleanup**: Operates under `write_disposition="replace"`, allowing IPs that drop below the confidence threshold or age out to be cleaned up automatically by Cognee's `orphan_cleanup`.

---

## Installation

```bash
pip install "cognee-community-connector-abuseipdb"
```

Or from the `cognee-community` monorepo:

```bash
cd packages/connector/abuseipdb
pip install -e .
```

---

## Quickstart

### 1. Ingest Suspicious IPs into Cognee

```python
import asyncio
import os
import cognee
from cognee_community_connector_abuseipdb import abuseipdb_source


async def main():
    # 1. Initialize connector with target IPs
    source = abuseipdb_source(
        api_key=os.environ.get("ABUSEIPDB_API_KEY"),
        ips=[
            "198.51.100.42",
            "203.0.113.19",
            "10.0.0.1",  # Automatically filtered (private IP)
        ],
        min_confidence=25,
    )

    # 2. Ingest into Cognee staging
    await cognee.add(source)

    # 3. Cognify: extract entities, relationships, and vector embeddings
    await cognee.cognify()

    # 4. Search agent memory
    results = await cognee.search(
        "Which external IPs have a high abuse confidence score and are known for SSH brute-force attacks?"
    )
    for res in results:
        print(res)


if __name__ == "__main__":
    asyncio.run(main())
```

---

### 2. Offline / Air-Gapped Snapshot Ingestion

```python
source = abuseipdb_source(
    snapshot_data="./threat_intel_snapshot.json",
    min_confidence=50,
)
```

---

## Document Schema & Metadata

Each IP reputation record is formatted as a structured Markdown document and tagged with:

| Field | Type | Description |
|---|---|---|
| `id` | `str` | IP address (e.g. `198.51.100.42`) |
| `ip` | `str` | Canonical IP address |
| `title` | `str` | Standardized header `[AbuseIPDB] <IP> (Confidence: <Score>%)` |
| `confidence_score` | `int` | Abuse confidence score (0-100%) |
| `country_code` | `str` | ISO country code |
| `isp` | `str` | Internet Service Provider |
| `domain` | `str` | Associated domain name |
| `is_tor` | `bool` | Whether the IP is a known Tor exit node |
| `total_reports` | `int` | Total count of distinct abuse reports |
| `content` | `str` | Structured Markdown body with attack categories & recommendations |

---

## Running Tests

Run the hermetic test suite:

```bash
pytest packages/connector/abuseipdb/tests/ -v
```
