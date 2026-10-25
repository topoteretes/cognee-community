# Cognee Community Connector: SurrealDB

Community data-source connector for [SurrealDB](https://surrealdb.com/) multi-model database, document models, and graph relation edges into [Cognee](https://github.com/topoteretes/cognee).

## Overview
SurrealDB is an end-to-end cloud and local multi-model database combining tables, documents, and graph relations into a unified architecture. Ingesting SurrealDB records and graph edges directly into Cognee enables AI agents to query interconnected data (e.g. *Which customer accounts bought product X?*, *What open support tickets link to organization Acme?*).

## Features
- **Multi-Model Support:** Ingests document tables, relational schemas, and graph relations (`in`/`out` pointers).
- **Dynamic Table Discovery:** Supports automatic table discovery via `INFO FOR DB;` or bounded specific table lists.
- **Flexible Authentication:** Supports both JWT Bearer tokens and username/password Basic authentication.
- **Graph-Ready Markdown Synthesis:** Formats records, custom fields, and relational graph edges into semantic markdown ready for LLM vectorization and entity extraction.
- **100% Hermetic Testing:** Tested offline with zero external network or database daemon requirements.

## Installation
```bash
pip install cognee-community-connector-surrealdb
```

## Quickstart
```python
import asyncio
from cognee_community_connector_surrealdb import SurrealDBConnector
import cognee

async def main():
    connector = SurrealDBConnector(
        namespace="my_namespace",
        database="my_database",
        url="http://localhost:8000",
        username="root",
        password="root",
        tables=["customer", "order", "purchased"],
        limit_per_table=50,
    )

    documents = await connector.fetch_all()
    print(f"Extracted {len(documents)} records and graph relationships.")

    for doc in documents:
        await cognee.add(doc["clean_text"])

    await cognee.cognify()
    print("Ingestion complete!")

if __name__ == "__main__":
    asyncio.run(main())
```
