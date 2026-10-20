# Databricks Data Connector for `cognee`

Data-source connector for ingesting assets from [Databricks](https://www.databricks.com) workspaces into `cognee` memory.

Built with [`dlt`](https://dlthub.com), this connector syncs Databricks workspace assets across three distinct resources:
1. **Notebooks (`databricks_notebooks`)**: Markdown and code documents (Python, SQL, Scala, R) ingested as documents into Cognee's cognify and graph extraction pipeline.
2. **Unity Catalog Tables (`databricks_tables`)**: Structured table metadata (catalogs, schemas, tables, columns, data types, and comments) with data mutation tracking via Delta table history (`DESCRIBE HISTORY`).
3. **SQL Queries (`databricks_queries`)**: Explicitly configured SQL queries executed on SQL Warehouses with chunked result pagination.

---

## Key Design Principles & Guardrails

### 1. Stable Identity via `object_id`
Notebook identity in Cognee is determined by the internal Databricks `object_id`:
```
databricks:<workspace_id>:notebook:<object_id>
```
If a notebook is moved, renamed, or organized into different workspace folders, its graph identity and existing relationships in Cognee remain intact.

### 2. Delta History vs. Metadata Change Detection
Table metadata from Unity Catalog reflects schema changes (e.g. column additions or comment updates), but not underlying row changes. For Delta tables, the connector executes `DESCRIBE HISTORY` and inspects data-altering operations:
- Tracks: `WRITE`, `UPDATE`, `DELETE`, `MERGE`.
- Ignores: Maintenance operations such as `OPTIMIZE` and `VACUUM`.

### 3. Safe Deletion Reconciliation
To prevent inadvertent deletions during transient network failures or rate limits:
- Deletion tombstones (`_deleted: True`) are **only** emitted when a 100% complete inventory of the configured workspace paths or catalogs succeeds.
- If any directory traversal, schema listing, or API request fails, **deletion reconciliation is aborted** and existing state is preserved.

### 4. Opt-In SQL Execution
SQL statements discovered inside notebooks are **never** executed automatically. Queries are executed only when explicitly configured by the caller via the `queries` parameter.

---

## Installation

Install the connector within your Python environment:

```bash
pip install cognee-community-connector-databricks
```

---

## Configuration & Credentials

Set the following environment variables or provide them directly to the `databricks_source` factory:

| Variable | Description |
| :--- | :--- |
| `DATABRICKS_HOST` | Workspace URL (e.g., `https://dbc-12345678-abcd.cloud.databricks.com`). |
| `DATABRICKS_TOKEN` | Personal Access Token with workspace and catalog permissions. |
| `DATABRICKS_WAREHOUSE_ID` | (Optional) SQL Warehouse ID for Delta history checks and explicit queries. |

---

## Quickstart

```python
import asyncio
import os
import cognee
from cognee_community_connector_databricks import databricks_source


async def main():
    # Configure the Databricks source
    source = databricks_source(
        host=os.getenv("DATABRICKS_HOST"),
        token=os.getenv("DATABRICKS_TOKEN"),
        include=["notebooks", "tables"],
        workspace_paths=["/Shared"],
        catalogs=["main"],
        warehouse_id=os.getenv("DATABRICKS_WAREHOUSE_ID"),
        write_disposition="merge",
    )

    # Ingest into cognee memory
    await cognee.remember(
        source,
        dataset_name="databricks_knowledge",
    )


if __name__ == "__main__":
    asyncio.run(main())
```

---

## API Reference

### `databricks_source(...)`

```python
def databricks_source(
    host: str | None = None,
    token: str | None = None,
    *,
    include: list[str] | None = None,
    workspace_paths: list[str] | None = None,
    catalogs: list[str] | None = None,
    queries: list[dict[str, Any]] | None = None,
    warehouse_id: str | None = None,
    write_disposition: str = "merge",
    client: Any = None,
) -> dlt.sources.DltSource: ...
```

| Parameter | Type | Default | Description |
| :--- | :--- | :--- | :--- |
| `host` | `str \| None` | `None` | Workspace host URL. Falls back to `DATABRICKS_HOST`. |
| `token` | `str \| None` | `None` | Personal Access Token. Falls back to `DATABRICKS_TOKEN`. Never logged. |
| `include` | `list[str] \| None` | `["notebooks", "tables", "queries"]` | Resources to include. |
| `workspace_paths` | `list[str] \| None` | `["/Shared"]` | Workspace folders to traverse for notebooks. |
| `catalogs` | `list[str] \| None` | `None` | List of Unity Catalog catalog names to sync (defaults to all). |
| `queries` | `list[dict[str, Any]] \| None` | `None` | Explicit SQL queries: `[{"name": ..., "statement": ..., "warehouse_id": ...}]`. |
| `warehouse_id` | `str \| None` | `None` | SQL warehouse ID for history checks and query execution. |
| `write_disposition` | `str` | `"merge"` | `dlt` sync disposition. |

---

## Running Offline Tests

The test suite runs 100% offline with zero live network dependencies:

```bash
python -m pytest packages/connector/databricks/tests -v
```
