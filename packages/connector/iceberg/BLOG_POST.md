# Giving AI Agents Lakehouse Memory: Building an Apache Iceberg Connector for Cognee

*Author: Soumyajit Ghosh (@somuai)*  
*Target Publication: Dev.to / Hashnode / Substack*  
*Hackathon: Mergetober (WeMakeDevs x Cognee)*  

---

## 1. The Blind Spot in Enterprise AI: Lakehouse Memory

Modern autonomous AI agents are rapidly moving from toy chat interfaces to mission-critical operational tools. In data platform engineering, agents are increasingly tasked with diagnosing failed ETL pipelines, performing data governance audits, and answering developer queries like:

> *"Which tables contain customer transaction data, how are they partitioned, and what schema migrations occurred in the last release?"*

However, most LLM applications remain blind to the storage foundation of the modern data stack: **The Data Lakehouse**. 

Over the past three years, **Apache Iceberg** has become the open table format standard powering enterprise lakehouses at Netflix, Apple, Snowflake, Databricks, BigQuery, Starburst, and AWS. Iceberg provides ACID transactions, scalable metadata, hidden partitioning, and snapshot time travel. 

Yet, when engineering teams plug AI agents into their infrastructure, agents have no semantic memory of these lakehouses. Traditional relational connectors either attempt to dump millions of raw table rows into context windows (which blows up tokens and leaks PII) or require cumbersome manual schema documentation that goes stale immediately.

To solve this, I built the **Apache Iceberg data-source connector for [Cognee](https://github.com/topoteretes/cognee)** as part of the **Mergetober Hackathon** ([topoteretes/cognee-community#348](https://github.com/topoteretes/cognee-community/pull/348), closing [`topoteretes/cognee#5553`](https://github.com/topoteretes/cognee/issues/5553)).

In this technical walkthrough, I will break down how Cognee's cognitive memory engine works, how we designed a zero-leakage metadata connector using `dlt` and `pyiceberg`, and how agents can use knowledge graphs to reason across evolving lakehouse schemas.

---

## 2. What is Cognee?

LLMs are inherently stateless. Each API request starts from scratch. Retrieval-Augmented Generation (RAG) helps, but naive vector search over chunked text often fails to capture relational hierarchies, schema dependencies, and operational changes.

**Cognee** (`topoteretes/cognee`) is an open-source memory engine for AI agents. Rather than treating data as isolated chunks, Cognee:
1. Ingests data through resilient pipelines powered by `dlt` (data load tool).
2. Runs **`cognify`**: an entity-extraction and graph-construction process that links concepts, relationships, and metadata into an interconnected Knowledge Graph (backed by FalkorDB, Neo4j, or NetworkX) alongside vector embeddings (LanceDB, Qdrant).
3. Provides graph-completion search (`cognee.search(..., query_type=SearchType.GRAPH_COMPLETION)`), allowing agents to traverse complex relationships and recall historical context.

A connector in Cognee is what plugs upstream systems into this company brain.

```
┌────────────────────────────────┐
│   Apache Iceberg Lakehouse     │
│  (REST / Glue / Hive Catalog)  │
└───────────────┬────────────────┘
                │
                ▼ pyiceberg
┌────────────────────────────────┐
│    Iceberg dlt Connector       │
│  - Table Schema & Comments     │
│  - Partition Specifications    │
│  - Snapshot Commit History     │
└───────────────┬────────────────┘
                │
                ▼ write_disposition="replace"
┌────────────────────────────────┐
│       Cognee Ingestion         │
│  (DOCUMENT_SOURCE_ATTR Routing)│
└───────────────┬────────────────┘
                │
                ▼ cognify()
┌────────────────────────────────┐
│     Knowledge Graph Memory     │
│    (Entities, Schema Edges,    │
│      Snapshot Provenance)      │
└───────────────┬────────────────┘
                │
                ▼ search()
┌────────────────────────────────┐
│ Autonomous AI Operations Agent │
└────────────────────────────────┘
```

---

## 3. Connector Architecture: Why Metadata & Document Mode?

When designing the Iceberg connector, we made two critical architectural decisions:

### A. Metadata-First Semantic Ingestion (No Raw Data Dumps)
A typical production Iceberg table contains petabytes of Parquet files. An AI agent does not need to read 500 million transaction rows to know how to write an optimal query or debug a pipeline. 

Instead, the connector reads **table metadata**:
- Full table identifier and namespace hierarchy (e.g., `analytics.finance.orders`).
- Column definitions: field IDs, names, nested types, nullability, and docstrings.
- Partition specifications: source fields and transforms (`identity`, `bucket`, `truncate`, `day`, `month`, `year`).
- Snapshot commit history: commit timestamps, operation types (`append`, `overwrite`, `delete`), and record summaries.
- Table properties and ownership metadata (with automatic secret redaction).

### B. Cognee Document-Mode Routing
In Cognee, tabular SQL data is often routed into rigid relational schema tables. However, lakehouse metadata is richest when treated as **architectural prose**.

By tagging our `dlt` source with Cognee's document-mode marker:
```python
from cognee.tasks.ingestion.dlt_utils import DOCUMENT_SOURCE_ATTR

source = _iceberg()
setattr(source, DOCUMENT_SOURCE_ATTR, "iceberg")
```
Cognee's ingestion pipeline routes each table into the full `cognify` pipeline. The LLM extracts semantic relationships (e.g., *`orders` table is partitioned by `created_at` day*, *`user_id` foreign key connects to `users`*), storing them as explicit graph nodes.

---

## 4. The Core Engineering Challenge: Full-Snapshot Sync and Forget-on-Delete

In production data platforms, tables are frequently altered, dropped, or decommissioned. A major failure mode in AI memory layers is **ghost knowledge**: an agent hallucinating that a table still exists weeks after data engineers dropped it.

To prevent this, the Iceberg connector implements a **full-snapshot replacement strategy**:

```python
@dlt.resource(name="iceberg_tables", primary_key="id", write_disposition="replace")
def iceberg_tables():
    count = 0
    for table in _iter_tables(catalog, namespaces=namespaces, table_names=table_names):
        count += 1
        yield _table_to_row(table, include_snapshots=include_snapshots)
    logger.info("Iceberg: synced %d table(s).", count)
```

### How Forget-on-Delete Works:
1. `write_disposition="replace"` ensures each sync run replaces staging with the exact set of tables currently visible in the catalog.
2. If a table `finance.temp_payroll` is dropped upstream, it simply disappears from the catalog listing.
3. On the next sync run, the table is absent from staging.
4. Cognee's internal `orphan_cleanup` detects the vanished entity and reconciles it out of both the knowledge graph and vector indices.
5. Unchanged tables maintain a stable content hash (`data_id`), ensuring they are **not re-cognified**, saving LLM token costs.
6. A render failure immediately aborts the run before staging commits, ensuring transient API blips never cause accidental data deletion.

---

## 5. Implementation Deep Dive

The connector lives in `packages/connector/iceberg/` inside `topoteretes/cognee-community`. Here is how the key components are implemented:

### Connecting to Any Iceberg Catalog
Using Apache's official `pyiceberg` client, the connector connects to standard REST Catalogs, AWS Glue, Hive Metastores, or Nessie:

```python
from pyiceberg.catalog import load_catalog

catalog_properties = {
    "type": "rest",
    "uri": "https://iceberg-catalog.prod.internal:8181",
    "warehouse": "s3://production-lakehouse/warehouse",
    "token": os.environ.get("ICEBERG_BEARER_TOKEN"),
}
catalog = load_catalog("production", **catalog_properties)
```

### Rendering Schemas and Commit Logs
Each table is formatted into structured, human-readable markdown:

```python
def _render_schema(schema: Any) -> str:
    """Render an Iceberg Schema into a clean markdown table."""
    if not schema or not hasattr(schema, "fields"):
        return "_No schema definition available._"

    lines = [
        "| Column ID | Field Name | Type | Required | Doc |",
        "| :--- | :--- | :--- | :--- | :--- |",
    ]
    for field in schema.fields:
        doc = field.doc or ""
        req = "Yes" if getattr(field, "required", False) else "No"
        row = f"| {field.field_id} | `{field.name}` | `{field.field_type}` | {req} | {doc} |"
        lines.append(row)
    return "\n".join(lines)
```

### Secret Redaction & Defensive Sanitization
To ensure security, table properties are scanned, and any keys matching sensitive terms (`token`, `secret`, `password`, `key`) are automatically redacted prior to ingestion:

```python
def _render_properties(properties: dict[str, Any]) -> str:
    lines = ["| Property Key | Value |", "| :--- | :--- |"]
    for k, v in sorted(properties.items()):
        if any(term in k.lower() for term in ["token", "secret", "password", "key"]):
            continue
        lines.append(f"| `{k}` | `{v}` |")
    return "\n".join(lines)
```

---

## 6. End-to-End Walkthrough: Querying Lakehouse Memory

Here is how simple it is to use the connector in an autonomous agent application:

```python
import asyncio
import os
import cognee
from cognee_community_connector_iceberg import iceberg_source

DATASET_NAME = "lakehouse_memory"

async def main():
    # 1. Configure Iceberg Catalog Source
    source = iceberg_source(
        catalog_properties={
            "type": "rest",
            "uri": "http://localhost:8181",
            "warehouse": "s3://lakehouse-data/warehouse",
        },
        namespaces=[("analytics",), ("finance",)],
        include_snapshots=True,
    )

    # 2. Sync into Cognee Memory
    print("Syncing Apache Iceberg lakehouse metadata into Cognee...")
    await cognee.remember(source, dataset_name=DATASET_NAME)

    # 3. Ask complex structural and architectural questions
    query = (
        "Which tables in the finance namespace are partitioned by day, "
        "what are their primary keys, and what write operations occurred recently?"
    )
    
    answer = await cognee.search(
        query_text=query,
        query_type=cognee.SearchType.GRAPH_COMPLETION,
        datasets=[DATASET_NAME],
    )
    print("\nAgent Answer:\n", answer)

if __name__ == "__main__":
    asyncio.run(main())
```

### Sample Output:
```text
The finance namespace contains two daily-partitioned tables:
1. `finance.orders`: Partitioned on `created_at` (transform: `day`). Primary key: `order_id` (string). Recent operations: 3 append commits adding 1.2M records, followed by 1 overwrite commit on partition 2026-10-01.
2. `finance.settlements`: Partitioned on `settled_at` (transform: `day`). Primary key: `settlement_id` (string).
```

---

## 7. Testing & Reliability in CI

Open-source connectors often fail in CI because tests rely on live credentials or paid cloud APIs.

Our test suite (`tests/test_iceberg.py`) solves this with **in-memory fake catalogs** (`FakeIcebergCatalog`, `FakeIcebergTable`):
- All 8 unit and integration tests run deterministically without internet access.
- Tests verify:
  1. Schema, partition transform, and snapshot markdown rendering.
  2. Transient vs. permanent error classification (`NoSuchTableError` is recognized as permanently gone, while HTTP 503 is retried).
  3. Full-snapshot reconciliation: verifying via an embedded DuckDB/SQLite pipeline that dropping a table from the catalog removes it from staging on the subsequent run.
- Executed in under 1.5 seconds with `pytest` and verified 100% clean with `ruff`.

---

## 8. Summary & What's Next

By bridging Apache Iceberg into Cognee:
- AI agents gain persistent, self-updating awareness of lakehouse architectures.
- Data engineers can query table histories, schemas, and partition specs using natural language.
- Decommissioned tables are automatically reconciled out of memory, eliminating hallucinations.

The code is available in pull request [topoteretes/cognee-community#348](https://github.com/topoteretes/cognee-community/pull/348) and fork branch [`somuai/cognee-community:feat/connector-iceberg`](https://github.com/somuai/cognee-community/tree/feat/connector-iceberg) under `packages/connector/iceberg/`, resolving [`topoteretes/cognee#5553`](https://github.com/topoteretes/cognee/issues/5553).

Let's build AI agents that actually understand enterprise data infrastructure!
