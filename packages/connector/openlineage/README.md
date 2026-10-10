# OpenLineage / Marquez Connector for Cognee

A `dlt`-based data-source connector that syncs data pipeline lineage, job topologies, execution run histories, and dataset schema facets from **OpenLineage** and **Marquez** into [Cognee](https://github.com/topoteretes/cognee)'s AI memory layer.

---

## The Problem: Why AI Agents Need Lineage Memory

Modern autonomous AI agents in platform and data engineering are increasingly asked questions such as:

> *"Which upstream Spark or Airflow job writes to the `retention_metrics` dataset?"*  
> *"Why did the finance reporting pipeline fail yesterday?"*  
> *"If we modify the schema of the `orders` table, which downstream pipelines and dashboards will break?"*

Without an operational lineage layer, LLMs have no graph visibility into end-to-end data provenance. OpenLineage is the Linux Foundation (LF AI & Data) open standard for operational lineage metadata, capturing run events across Apache Airflow, Apache Spark, dbt, Flink, and Trino.

This connector ingests OpenLineage job topologies, input/output datasets, schema facets, and run statuses directly into Cognee's knowledge graph.

---

## Key Features

- **Document-Mode Routing**: Declares `cognee_document_source = "openlineage"`. Ingested pipeline topologies bypass raw tabular relational tables and flow directly into Cognee's `cognify` entity-extraction pipeline, generating interconnected knowledge graph nodes linking jobs, runs, datasets, and schema definitions.
- **Semantic Lineage Formatting**: Renders pipeline relationships, dataset schema facets (column names, types, descriptions), and recent run histories into structured architectural Markdown.
- **Full-Snapshot Sync & Forget-on-Delete**: Emits pipeline items with `write_disposition="replace"`. Deprecated or decommissioned pipelines disappear on subsequent sync runs and are automatically pruned from Cognee knowledge graphs and vector stores via `orphan_cleanup`.
- **Zero Token Waste**: Unchanged jobs maintain stable content hashes (`data_id`), avoiding redundant LLM processing.
- **Enterprise Security**: Defensively redacts sensitive properties (matching `token`, `secret`, `password`, `key`) prior to ingestion.
- **Zero-Cloud-Dependency CI**: Tested offline against an in-memory `FakeMarquezClient` with 100% test pass rates and zero external network calls.

---

## Installation

Install the connector within your Cognee environment:

```bash
pip install cognee-community-connector-openlineage
```

Or from source:

```bash
uv pip install -e packages/connector/openlineage
```

---

## Quick Start

```python
import asyncio
import os
import cognee
from cognee_community_connector_openlineage import openlineage_source

DATASET_NAME = "pipeline_lineage_memory"

async def main():
    # 1. Configure the OpenLineage source pointing to Marquez or OpenLineage HTTP backend
    source = openlineage_source(
        endpoint_url=os.environ.get("OPENLINEAGE_URL", "http://localhost:5000"),
        api_key=os.environ.get("OPENLINEAGE_API_KEY"),
        namespaces=["analytics", "finance"],
        include_facets=True,
        max_runs_per_job=5,
    )

    # 2. Sync into Cognee Memory
    print("Syncing OpenLineage pipeline topologies into Cognee...")
    await cognee.remember(source, dataset_name=DATASET_NAME)

    # 3. Query the knowledge graph using Graph Completion
    query = (
        "Which upstream jobs populate the retention_metrics dataset, "
        "and did any recent runs fail with OutOfMemoryError?"
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

---

## Testing

All tests run deterministically offline using in-memory mock clients:

```bash
pytest packages/connector/openlineage/tests
```

To run linting:

```bash
ruff check packages/connector/openlineage
ruff format --check packages/connector/openlineage
```

---

## DCO Affirmation

All contributions conform to the Topoteretes Developer Certificate of Origin.
