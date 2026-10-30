# Directus Connector for Cognee

A data-source connector for [Cognee](https://github.com/topoteretes/cognee) that syncs collections and items from [Directus](https://directus.io/) into Cognee's persistent memory layer.

## Overview

- **Document Pipeline**: Collection items are mapped into clean document entities with stable IDs and indexed via Cognee's `cognify` entity extraction pipeline.
- **Full Snapshot Sync**: Uses `write_disposition="replace"`, allowing seamless propagation of deletions via Cognee's `orphan_cleanup` subsystem ("forget-on-delete").
- **Reliable Pagination & Retries**: Built-in retry handling for rate limits (429) with `Retry-After` header parsing and exponential backoff.
- **Streaming Generator**: Memory-efficient pagination via `limit` / `offset` without loading whole datasets into memory.
- **Security Guardrails**: System collections (`directus_*`) and sensitive auth fields (`password`, `token`, `tfa_secret`, etc.) are automatically excluded.

## Installation

```bash
pip install cognee-community-connector-directus
```

## Quick Start

```python
import asyncio
import os
import cognee
from cognee_community_connector_directus import directus_source

async def main():
    source = directus_source(
        base_url=os.environ.get("DIRECTUS_URL", "http://127.0.0.1:8055"),
        auth_token=os.environ.get("DIRECTUS_TOKEN"),
        collections=["articles", "faq"],
    )

    # Ingest and process into Cognee memory
    await cognee.add(source)
    await cognee.cognify()

    # Search knowledge graph
    results = await cognee.search("What is our refund policy?")
    print(results)

if __name__ == "__main__":
    asyncio.run(main())
```

## Configuration

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `base_url` | `str` | `http://127.0.0.1:8055` or `DIRECTUS_URL` | Base URL of your Directus instance |
| `auth_token` | `str` | `None` or `DIRECTUS_TOKEN` | Directus API token (optional for public collections) |
| `collections` | `list[str]` | `None` (auto-discover all) | Specific collection names to sync |
| `sort` | `str` | `"-date_updated"` | Sorting field for pagination |
| `fields` | `str` | `"*"` | Comma-separated fields to retrieve |
| `ignored_fields`| `set[str]` | `None` | Optional custom sensitive fields to exclude |

## Running Tests

```bash
uv run pytest packages/connector/directus/tests/
```
