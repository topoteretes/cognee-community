# Codecov Coverage-History Connector for Cognee

A production-ready data-source connector for **[Codecov](https://codecov.io/)** (API v2).

Syncs repository test coverage totals, branch trends, commit reports, and file-level coverage gaps directly into Cognee's semantic memory graph.

## Features

- **Repository & Commit Coverage Summaries**: Extracts coverage ratings, executable lines, hits, misses, and branch tracking from Codecov API v2.
- **Critical File Gap Identification**: Highlights untested source files and coverage drops to allow RAG agents to assist in prioritizing new test suites.
- **Pending & Missing Safety**: Safely distinguishes missing or pending reports (`PENDING / NO REPORT`) from a genuine 0% coverage result.
- **Graph Grouping without LLM Processing**: Generates `cognee_node_set` entries for repositories (`codecov:repo:...`), commits (`codecov:commit:...`), and branches (`codecov:branch:...`).
- **Offline Snapshot Support**: Supports offline JSON snapshots for deterministic, air-gapped CI verification.
- **DLT Full-Snapshot Replace Sync**: Guaranteed deterministic updates with SHA-256 document hashing (`DOCUMENT_SOURCE_ATTR = "codecov"`).

## Installation

```bash
pip install cognee-community-connector-codecov
```

## Quick Start

### 1. Ingest via Cognee Python API

```python
import cognee
from cognee_community_connector_codecov import codecov_source

# Option A: Live Codecov API
source = codecov_source(
    service="github",
    owner="your-org",
    repo="your-repo",
    branch="main",
    api_token="your-codecov-api-token",
    history_limit=15,
)

# Option B: Offline JSON snapshot file
source = codecov_source(snapshot_data="/path/to/codecov_snapshot.json")

# Ingest into Cognee
cognee.add(source)
cognee.cognify()
```

### 2. Querying Agent Memory

Once cognified, agents can answer queries such as:
- *"Which files had the lowest test coverage in our latest commit?"*
- *"Did coverage improve between commit 7b8a9c0d and previous releases?"*
- *"Show all files with more than 50 missed lines across the repository."*

## Testing

Run unit tests hermetically:

```bash
pytest packages/connector/codecov/tests/ -v
```
