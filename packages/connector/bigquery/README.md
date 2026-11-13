# cognee-community-connector-bigquery

A BigQuery data-source connector for [cognee](https://github.com/topoteretes/cognee):
sync BigQuery table/column descriptions, schemas, and query results into memory — "chat with your data warehouse".

Exposes a `dlt` resource passed directly to `cognee.remember(...)` / `cognee.add(...)`.
Schemas and records are rendered into structured documents and ingested via document mode,
meaning they flow through cognee''s `cognify` entity-extraction pipeline directly into
the knowledge graph.

## Features

- **Service Account Auth**: Non-interactive auth via Service Account JSON key (`credentials_path` or `credentials_info`), environment variables, or Application Default Credentials.
- **Metadata Ingestion**: Ingests table descriptions, column names, data types, modes (`NULLABLE`, `REQUIRED`, `REPEATED`), and descriptions. Table metadata is often the highest-leverage signal for an AI memory system.
- **Query & Row Results**: Ingests query results or table rows as structured text documents.
- **Incremental Sync**: Uses table modified timestamps for metadata and partition or timestamp columns (`incremental_column`) for rows, persisting cursors in `dlt` state so re-running only syncs deltas.
- **Forget-on-Delete**: Dropped or deleted tables in BigQuery are detected on the next sync and emitted with `_deleted=True` tombstones so cognee''s orphan cleanup purges them from the knowledge graph and vector stores.

## Install

```bash
uv pip install cognee-community-connector-bigquery
# or from this monorepo:
cd packages/connector/bigquery && uv sync --all-extras
```

## Quickstart

```python
import cognee
from cognee_community_connector_bigquery import bigquery_source

# Sync dataset schemas and column descriptions into cognee memory
await cognee.remember(
    bigquery_source(
        dataset_id="analytics",
        credentials_path="/path/to/service-account.json",
    ),
    dataset_name="bigquery_catalog",
)

# Ask questions about your database schema
answer = await cognee.search(
    query_text="Which table contains customer lifetime value and what is its schema?",
    query_type=cognee.SearchType.GRAPH_COMPLETION,
    datasets=["bigquery_catalog"],
)
print(answer)
```

## Incremental Row Ingestion

```python
# Sync table rows incrementally using an updated_at timestamp column
await cognee.remember(
    bigquery_source(
        dataset_id="analytics",
        table_names=["orders"],
        include_rows=True,
        incremental_column="updated_at",
        primary_key="order_id",
    ),
    dataset_name="orders_memory",
    write_disposition="merge",
)
```

## Testing

Run tests without requiring live GCP credentials:

```bash
uv run pytest tests/
```
