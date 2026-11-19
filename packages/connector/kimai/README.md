# Kimai Connector for Cognee

A production-ready data-source connector for **[Kimai](https://www.kimai.org/)**, the self-hosted open-source time-tracking platform.

This connector allows AI agents and Cognee RAG workflows to index client accounts, projects, activities, and completed timesheets directly into semantic memory graphs.

## Features

- **Entity Hierarchy Extraction**: Indexes Customers (`/api/customers`), Projects (`/api/projects`), Activities (`/api/activities`), and Timesheets (`/api/timesheets`).
- **Graph Grouping without LLM Pre-processing**: Automatically generates `cognee_node_set` links (`customer:<id>`, `project:<id>`, `activity:<id>`, `user:<id>`) namespaced by the Kimai instance host.
- **Privacy & Rate Protection**: Hourly rates and financial figures are strictly excluded by default (`include_rates=False`). Contributor emails are omitted, preserving team privacy.
- **Running Timer Filtering**: Active running timers (`end is None`) are safely omitted from searchable text until stopped, while retaining tracking IDs for reconciliation.
- **Offline Snapshot & Live API Modes**: Supports both offline JSON snapshots (for testing and air-gapped CI) and live API ingestion.
- **DLT Full-Snapshot Sync**: Uses replace write disposition with deterministic SHA-256 content hashes.

## Installation

```bash
pip install cognee-community-connector-kimai
```

## Quick Start

### 1. Ingest via Cognee Python API

```python
import cognee
from cognee_community_connector_kimai import kimai_source

# Option A: Live Kimai instance
source = kimai_source(
    base_url="https://kimai.yourdomain.com",
    api_token="your-kimai-api-token",
    since="2026-01-01T00:00:00",
    include_rates=False,
)

# Option B: Offline snapshot file
source = kimai_source(snapshot_data="/path/to/kimai_snapshot.json")

# Ingest into Cognee
cognee.add(source)
cognee.cognify()
```

### 2. Querying Agent Memory

Once cognified, agents can answer queries such as:
- *"Summarize what work was shipped for Acme Corp in September and who worked on it."*
- *"Which contributors have logged hours on the Billing Migration project?"*
- *"Show all completed engineering activities under Project #10."*

## Testing

Run unit tests hermetically:

```bash
pytest packages/connector/kimai/tests/ -v
```
