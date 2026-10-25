# Cognee Community Connector: MotherDuck

Community data-source connector for [MotherDuck](https://motherduck.com/) serverless DuckDB databases, shares, and query execution telemetry into [Cognee](https://github.com/topoteretes/cognee).

## Overview
MotherDuck enables serverless DuckDB analytics in the cloud. Connecting database catalogs, shared datasets, and query execution histories directly into Cognee enables autonomous AI copilots to answer complex systems and telemetry questions (e.g. *What were the longest running queries executed on our analytical warehouse today?*, *Which shared datasets are currently available for our pipelines?*).

## Features
- **Database Catalog Ingestion:** Ingests active databases, sizes, owners, sharing states, and modification timestamps.
- **Data Sharing Visibility:** Captures database shares, linked schemas, and access permissions.
- **Bounded Query Telemetry:** Collects recent executed queries, execution durations, status codes (`SUCCESS`, `FAILED`), and rows scanned with strict limit guards to prevent graph bloat.
- **Clean Semantic Document Mode:** Automatically formats catalogs and SQL snippets into semantic markdown ready for LLM vectorization.
- **100% Hermetic Testing:** Tested offline with zero live cloud dependencies.

## Installation
```bash
pip install cognee-community-connector-motherduck
```

## Quickstart
```python
import asyncio
from cognee_community_connector_motherduck import MotherDuckConnector
import cognee

async def main():
    connector = MotherDuckConnector(
        token="your_motherduck_token",
        fetch_queries=True,
        query_limit=25,
    )

    data = await connector.fetch_all()
    print(f"Retrieved {len(data['databases'])} databases and {len(data['queries'])} queries.")

    # Ingest database schemas and query telemetry into Cognee
    for db in data["databases"]:
        await cognee.add(db["clean_text"])
    
    for query in data["queries"]:
        await cognee.add(query["clean_text"])

    await cognee.cognify()
    print("Ingestion complete!")

if __name__ == "__main__":
    asyncio.run(main())
```
