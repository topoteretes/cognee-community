# cognee-community-connector-unitycatalog

Databricks Unity Catalog data-source connector for [cognee](https://github.com/topoteretes/cognee).

Syncs enterprise data lakehouse governance, catalogs, schemas, tables/views with column definitions, and registered AI/ML models into Cognee memory and knowledge graphs.

## Features

- **Lakehouse Metadata Hierarchy**: Ingests catalogs, schemas, tables, and views across Managed Delta, External, and View table types.
- **Column Schema & Data Types**: Extracts column names, data types (STRING, DECIMAL, TIMESTAMP, STRUCT), comments, and partition columns into searchable knowledge graph entities.
- **AI/ML Model Registry**: Ingests registered models, alias versions (e.g. `champion`, `challenger`), run links, and performance tag metadata.
- **Forget-On-Delete**: Uses `write_disposition="replace"` and `cognee_document_source = "unitycatalog"` so dropped or decommissioned tables and models automatically prune from Cognee via orphan cleanup.
- **Selective Scoping**: Filter ingestion to specific catalogs and schemas.
- **Security & Redaction**: Automatic Bearer token and PAT credential redaction in all logs and exception traces.

## Installation

```bash
pip install cognee-community-connector-unitycatalog
```

## Quickstart

```python
import asyncio
import cognee
from cognee_community_connector_unitycatalog import unitycatalog_source


async def main():
    # Ingest lakehouse catalogs and tables
    source = unitycatalog_source(
        base_url="https://adb-1234.azuredatabricks.net",
        token="your-databricks-pat-token",
        catalogs=["production", "analytics"],
        include_tables=True,
        include_models=True,
    )

    await cognee.add(source)
    await cognee.cognify()

    # Query your data assets and AI models
    results = await cognee.search(
        "What columns exist in the production.finance.settled_transactions table?"
    )
    print(results)


if __name__ == "__main__":
    asyncio.run(main())
```

## Environment Variables

- `DATABRICKS_HOST` / `UNITY_CATALOG_URL`: Databricks workspace URL or open-source Unity Catalog server URL.
- `DATABRICKS_TOKEN` / `UNITY_CATALOG_TOKEN`: Databricks Personal Access Token (PAT) or Unity Catalog Bearer token.
