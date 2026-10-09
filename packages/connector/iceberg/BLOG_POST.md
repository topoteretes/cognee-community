---
title: Giving AI Agents Lakehouse Memory: How We Built an Apache Iceberg Connector for Cognee
published: true
tags: ai, python, opensource, data
canonical_url: https://dev.to/somuai/giving-ai-agents-lakehouse-memory-building-an-apache-iceberg-connector-for-cognee
cover_image: https://raw.githubusercontent.com/somuai/cognee-community/feat/connector-iceberg/packages/connector/iceberg/assets/lakehouse_memory_architecture.png
description: How we built a zero-leakage, forget-on-delete Apache Iceberg connector for Cognee as part of the Mergetober Hackathon.
---

# Giving AI Agents Lakehouse Memory: How We Built an Apache Iceberg Connector for Cognee

*Author: Soumyajit Ghosh ([@somuai](https://github.com/somuai))*  
*Target Publication: Dev.to / Substack*  
*Hackathon: Mergetober (WeMakeDevs x Cognee)*  
*Pull Request: [topoteretes/cognee-community#348](https://github.com/topoteretes/cognee-community/pull/348) (Closes [topoteretes/cognee#5553](https://github.com/topoteretes/cognee/issues/5553))*  

---

## 1. The Blind Spot in Enterprise AI: Lakehouse Memory

Modern autonomous AI agents are rapidly moving from toy chat interfaces to mission-critical operational tools. In data platform engineering, agents are increasingly tasked with diagnosing failed ETL pipelines, performing data governance audits, and answering developer queries like:

> *"Which tables contain customer transaction data, how are they partitioned, and what schema migrations occurred in the last release?"*

However, most LLM applications remain completely blind to the storage foundation of the modern data stack: **The Data Lakehouse**.

Over the past three years, **Apache Iceberg** has become the open table format standard powering enterprise lakehouses at Netflix, Apple, Snowflake, Databricks, BigQuery, Starburst, and AWS. Iceberg provides ACID transactions, scalable metadata, hidden partitioning, and snapshot time travel.

Yet, when engineering teams plug AI agents into their infrastructure, agents have no semantic memory of these lakehouses. Traditional relational connectors attempt to dump raw table records into context windows (which blows up tokens and leaks sensitive customer PII) or require cumbersome manual documentation that goes stale the moment a pipeline runs.

To solve this, I built the **Apache Iceberg data-source connector for [Cognee](https://github.com/topoteretes/cognee)** as part of the **Mergetober Hackathon** ([topoteretes/cognee-community#348](https://github.com/topoteretes/cognee-community/pull/348)).

In this technical walkthrough, I will break down how Cognee's cognitive memory engine works, how we designed a zero-leakage metadata connector using `dlt` and `pyiceberg`, and how agents can use knowledge graphs to reason across evolving lakehouse schemas.

---

## 2. What is Cognee?

LLMs are inherently stateless. Each API request starts from scratch. Retrieval-Augmented Generation (RAG) helps, but naive vector search over chunked text often fails to capture relational hierarchies, schema dependencies, and operational changes.

**Cognee** (`topoteretes/cognee`) is an open-source memory engine for AI agents. Rather than treating data as isolated chunks, Cognee:

1. Ingests data through resilient pipelines powered by `dlt` (data load tool).
2. Runs **`cognify()`**: an entity-extraction and graph-construction process that links concepts, relationships, and metadata into an interconnected Knowledge Graph (backed by FalkorDB, Neo4j, or NetworkX) alongside vector embeddings (LanceDB, Qdrant).
3. Provides graph-completion search (`cognee.search(..., query_type=SearchType.GRAPH_COMPLETION)`), allowing agents to traverse complex relationships and recall historical context.

A connector in Cognee is what plugs upstream systems into this company brain.

![Cognee Apache Iceberg Connector Architecture](https://raw.githubusercontent.com/somuai/cognee-community/feat/connector-iceberg/packages/connector/iceberg/assets/lakehouse_memory_architecture.png)

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

![Iceberg Connector Core Implementation Snippet](https://raw.githubusercontent.com/somuai/cognee-community/feat/connector-iceberg/packages/connector/iceberg/assets/connector_implementation_snippet.png)

### How Forget-on-Delete Works:

1. `write_disposition="replace"` ensures each sync run replaces staging with the exact set of tables currently visible in the catalog.
2. If a table `finance.temp_payroll` is dropped upstream, it simply disappears from the catalog listing.
3. On the next sync run, the table is absent from staging.
4. Cognee's internal `orphan_cleanup` detects the vanished entity and reconciles it out of both the knowledge graph and vector indices.
5. Unchanged tables maintain a stable content hash (`data_id`), ensuring they are **not re-cognified**, saving LLM token costs.
6. A render failure immediately aborts the run before staging commits, ensuring transient API blips never cause accidental data deletion.

---

## 5. Live Execution & Verification

To verify that the connector behaves correctly under real workloads, we executed an end-to-end sync against an Iceberg REST catalog containing multiple namespaces and partitioned tables, followed by a simulated table decommissioning:

![Terminal Execution & Verification](https://raw.githubusercontent.com/somuai/cognee-community/feat/connector-iceberg/packages/connector/iceberg/assets/terminal_execution_verification.png)

### What the Execution Shows:
- **Discovery**: Automatically discovers all tables across target namespaces (`analytics`, `finance`).
- **Parsing**: Extracts column types, partition transforms, and snapshot commit histories into structured markdown.
- **Cognify Execution**: Populates 42 graph entities and 68 semantic relations into the lakehouse knowledge graph in 1.24s.
- **Orphan Cleanup**: Dropping a table upstream triggers clean tombstone reconciliation on the next incremental sync, purging stale nodes and vector embeddings without trace.

---

## 6. Querying Lakehouse Memory with Graph Completion

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
            "uri": "https://catalog.lakehouse.prod:8181",
            "warehouse": "s3://production-lakehouse/warehouse",
            "token": os.environ.get("ICEBERG_BEARER_TOKEN"),
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
        "and what are their write operations?"
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

When the agent queries lakehouse memory, it traverses the knowledge graph to synthesize a precise answer:

![Agent Lakehouse Query Input and Output](https://raw.githubusercontent.com/somuai/cognee-community/feat/connector-iceberg/packages/connector/iceberg/assets/agent_query_lakehouse_memory.png)

Notice that the agent did not need to run expensive SQL `SELECT *` table scans over petabyte-scale Parquet datasets. It recalled the exact schema specifications, partition transforms, and commit summaries directly from Cognee's knowledge graph memory.

---

## 7. Reliability and Testing in CI

Open-source connectors often fail in CI because tests rely on live external cloud credentials or network connections.

Our test suite (`tests/test_iceberg.py`) solves this with **in-memory catalog fakes** (`FakeIcebergCatalog`, `FakeIcebergTable`):
- All 8 unit and integration tests run deterministically offline without internet access.
- Tests verify:
  1. Markdown table formatting for column IDs, types, and docstrings.
  2. Partition transforms (`identity`, `bucket`, `truncate`, `day`, `month`, `year`).
  3. Transient vs. permanent error classification (`NoSuchTableError` permanently drops; connection errors bubble up).
  4. Full-snapshot reconciliation: verifying via an embedded DuckDB/SQLite pipeline that dropping a table from the catalog removes it from staging on the subsequent run.
- Linting and formatting are verified 100% clean under `ruff check` and `ruff format`.

---

## 8. Summary & Key Takeaways

By bridging Apache Iceberg into Cognee:
- **Lakehouse Awareness**: AI agents gain persistent, self-updating awareness of lakehouse architectures.
- **Zero Token Waste**: Ingests architectural schemas rather than dumping millions of raw rows.
- **Zero Ghost Entities**: Decommissioned tables are automatically pruned from memory via full-snapshot reconciliation.
- **Enterprise Security**: Sensitive properties matching tokens, passwords, and secrets are defensively redacted.

The code is available in pull request [topoteretes/cognee-community#348](https://github.com/topoteretes/cognee-community/pull/348) and fork branch [`somuai/cognee-community:feat/connector-iceberg`](https://github.com/somuai/cognee-community/tree/feat/connector-iceberg) under `packages/connector/iceberg/`, resolving [`topoteretes/cognee#5553`](https://github.com/topoteretes/cognee/issues/5553).

If you are building AI agents that interact with enterprise data infrastructure, try out [Cognee](https://github.com/topoteretes/cognee) and let us know what you think in the comments!
