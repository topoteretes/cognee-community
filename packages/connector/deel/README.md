# cognee-community-connector-deel

Deel data-source connector for **[Cognee](https://github.com/topoteretes/cognee)** — sync your organization's worker directory and contracts into AI memory with incremental sync and forget-on-delete.

## Overview

Deel manages global teams, contractors, EOR employees, and payroll contracts. This connector transforms your organization's Deel workforce directory and contract metadata into searchable knowledge graph documents within Cognee, enabling AI agents to answer workforce questions like:
- *"Who is currently working in the European engineering division?"*
- *"Which contractors have contracts up for renewal soon?"*
- *"What is our team distribution across different departments?"*

## Features

- **Document Mode**: Emits standard Cognee document items so that records flow through the full `cognify` entity-extraction pipeline and knowledge graph, rather than raw relational table dumps.
- **Privacy & Sensitivity First**: Contract terms, compensation, and legal clauses are sensitive. The connector ingests **sanitized metadata by default**. Full contract document bodies/clauses are strictly opt-in via `include_contract_documents=True`.
- **Full Snapshot & Forget-on-Delete**: Resources use `write_disposition="replace"`. When a worker or contract is deleted or terminated upstream, it drops out of the snapshot, allowing Cognee's `orphan_cleanup` to purge it from graph and vector stores.
- **Incremental Sync**: Filter updates with `since` watermark based on contract `updated_at`. Unchanged records retain stable content IDs and are not re-cognified.
- **Adaptive Pagination & Resiliency**: Handles both cursor-based (`after_cursor`) and offset-based (`offset`/`limit`) Deel API pagination, with built-in retry backoff on HTTP 429 and transient errors.

## Installation

```bash
uv pip install cognee-community-connector-deel
# OR
pip install cognee-community-connector-deel
```

## Authentication

Generate an API token from your Deel Organization dashboard:
1. Navigate to **Organization Settings** → **Apps & Integrations** → **Developer Center** → **Access Tokens**.
2. Create a personal or organization API token.
3. Export your token in your environment:

```bash
export DEEL_API_TOKEN="your_deel_api_token"
```

## Quickstart

```python
import asyncio
import cognee
from cognee_community_connector_deel import deel_source


async def main():
    # Ingest worker directory metadata and contract summaries
    source = deel_source(
        include_workers=True,
        include_contracts=True,
        include_contract_documents=False,  # Privacy default: metadata only
    )

    await cognee.remember(source, dataset_name="deel_org")

    # Query the memory graph
    results = await cognee.search(
        "Who are our active software engineers?",
        dataset_name="deel_org",
    )
    print(results)


if __name__ == "__main__":
    asyncio.run(main())
```

## Configuration & Parameters

| Parameter | Type | Default | Description |
|---|---|---|---|
| `token` | `str \| None` | `None` | Deel API token. Falls back to `DEEL_API_TOKEN` env var. |
| `base_url` | `str` | `"https://api.letsdeel.com/rest/v1"` | Deel API endpoint (can be set to sandbox `https://api-demo.letsdeel.com/rest/v1`). |
| `include_workers` | `bool` | `True` | Sync people directory (name, title, department, email, country, status). |
| `include_contracts` | `bool` | `True` | Sync contracts (title, type, worker, status, dates). |
| `include_contract_documents` | `bool` | `False` | **Privacy flag**: Opt-in to fetch and ingest full contract clauses/document bodies. |
| `worker_ids` | `list[str] \| None` | `None` | Restrict ingestion to specific worker IDs. |
| `contract_ids` | `list[str] \| None` | `None` | Restrict ingestion to specific contract IDs. |
| `contract_types` | `list[str] \| None` | `None` | Filter by contract types (e.g., `["eor", "fixed", "milestone"]`). |
| `contract_statuses` | `list[str] \| None` | `None` | Filter by status (e.g., `["in_progress", "completed"]`). |
| `since` | `str \| None` | `None` | ISO-8601 timestamp string for incremental sync (`updated_at >= since`). Automatically managed across runs via dlt resource state if omitted. |
| `client` | `Any` | `None` | Optional pre-configured client for dependency injection in tests. |

## Running Tests

Run the test suite using `uv`:

```bash
uv --directory packages/connector/deel run --with pytest pytest tests
```
