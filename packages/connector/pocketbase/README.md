# PocketBase Connector for Cognee

A data-source connector for [Cognee](https://github.com/topoteretes/cognee) that ingests records from [PocketBase](https://pocketbase.io/) collections into Cognee's persistent memory layer.

## Overview

- **Document Pipeline**: Records are formatted into clean document entities with stable IDs and indexed via Cognee's `cognify` entity extraction pipeline.
- **Full Snapshot Sync**: Uses `write_disposition="replace"`, allowing seamless propagation of deletions via Cognee's `orphan_cleanup`.
- **Reliable Pagination & Retries**: Built-in retry handling for rate limits (429) and transient errors.

## Installation

```bash
pip install cognee-community-connector-pocketbase
```

## Quick Start

```python
import asyncio
import os
import cognee
from cognee_community_connector_pocketbase import pocketbase_source

async def main():
    source = pocketbase_source(
        base_url=os.environ.get("POCKETBASE_URL", "http://127.0.0.1:8090"),
        auth_token=os.environ.get("POCKETBASE_TOKEN"),
        collections=["notes", "projects"],
    )

    # Ingest and process into Cognee memory
    await cognee.add(source)
    await cognee.cognify()

    # Search knowledge graph
    results = await cognee.search("What tasks are pending in projects?")
    print(results)

if __name__ == "__main__":
    asyncio.run(main())
```

## Configuration

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `base_url` | `str` | `http://127.0.0.1:8090` or `POCKETBASE_URL` | Base URL of your PocketBase instance |
| `auth_token` | `str` | `None` or `POCKETBASE_TOKEN` | Admin or user auth token (optional for public collections) |
| `collections` | `list[str]` | `None` (auto-discover all) | Specific collection names to sync |

## Running Tests

```bash
uv run pytest packages/connector/pocketbase/tests/
```
