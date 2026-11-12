# HydraDB / HydeaDB Connector for Cognee

A high-performance community data-source connector for **Cognee** that ingests knowledge records, documentation, and user memories from **HydraDB** into Cognee's entity extraction and knowledge graph memory layer.

## Overview

- **One-Way Ingestion**: Ingests context documents from HydraDB databases and collections directly into Cognee.
- **Document-Mode Routing**: Declares `cognee_document_source = "hydradb"` so records flow through Cognee's natural-language Cognify entity and relationship extraction pipeline.
- **Incremental & Merge-Safe**: Deterministic document identifiers (`hydradb:{database}:{collection}:{context_id}`) and SHA-256 content fingerprints avoid redundant LLM extraction and cognification across repeat runs.
- **Resilient & Paginated**: Automatic pagination, rate-limit backoff handling (HTTP 429), and granular error reporting for missing or unavailable records.

## Installation

```bash
pip install cognee-community-connector-hydradb
```

Or install with `uv`:

```bash
uv add cognee-community-connector-hydradb
```

## Quickstart

```python
import asyncio
import cognee
from cognee_community_connector_hydradb import hydradb_source

async def main():
    # Ingest documents from HydraDB into Cognee memory
    await cognee.remember(
        hydradb_source(
            api_key="your_hydradb_api_key",
            database_id="knowledge_base",
            collection_id="technical_docs",
            category="knowledge",
        ),
        dataset_name="hydradb_docs",
    )

    # Search and ask questions over the ingested knowledge graph
    search_results = await cognee.search(
        "What are the core system architecture guidelines?",
        dataset_name="hydradb_docs",
    )
    print(search_results)

if __name__ == "__main__":
    asyncio.run(main())
```

## Configuration

| Parameter | Type | Default | Description |
|---|---|---|---|
| `api_key` | `str` | `os.environ["HYDRADB_API_KEY"]` | HydraDB Bearer API key |
| `database_id` | `str` | `None` | Restrict extraction to a specific database |
| `collection_id` | `str` | `None` | Restrict extraction to a specific collection |
| `category` | `str` | `None` | Filter by category (`knowledge`, `memories`, `specs`) |
| `client` | `HydraDBClient` | `None` | Optional pre-configured client instance |

## Testing

Run unit tests locally:

```bash
pytest packages/connector/hydradb/tests -v
```
