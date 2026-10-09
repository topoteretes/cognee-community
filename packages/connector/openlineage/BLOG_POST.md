---
title: Giving AI Agents Pipeline Lineage Memory: How We Built an OpenLineage Connector for Cognee
published: true
tags: ai, python, opensource, data
canonical_url: https://dev.to/soumyajit_ghosh_b93618199/giving-ai-agents-pipeline-lineage-memory-how-we-built-an-openlineage-connector-for-cognee-1ood
cover_image: https://raw.githubusercontent.com/somuai/cognee-community/feat/connector-openlineage/packages/connector/openlineage/assets/openlineage_memory_architecture.png
description: How we built an OpenLineage and Marquez data-source connector for Cognee to give autonomous AI agents end-to-end data pipeline lineage memory as part of the Mergetober Hackathon.
---

# Giving AI Agents Pipeline Lineage Memory: How We Built an OpenLineage Connector for Cognee

*Author: Soumyajit Ghosh ([@somuai](https://github.com/somuai))*  
*Target Publication: Dev.to / Substack*  
*Hackathon: Mergetober (WeMakeDevs x Cognee)*  
*Pull Request: [topoteretes/cognee-community#349](https://github.com/topoteretes/cognee-community/pull/349) (Closes [topoteretes/cognee#5554](https://github.com/topoteretes/cognee/issues/5554))*  

---

## 1. The Blind Spot in Enterprise AI: Pipeline Lineage Memory

Modern autonomous AI agents are rapidly evolving into operational engineering partners. In data platform engineering, agents are increasingly tasked with investigating broken ETL workflows, auditing dataset provenance, and answering critical questions:

> *"Which upstream Apache Spark job populates the `retention_metrics` table?"*  
> *"Why did the finance reporting pipeline fail yesterday?"*  
> *"If we modify the schema of the `orders` table, which downstream pipelines and dashboards will break?"*

However, most LLM applications possess zero awareness of operational lineage. They cannot observe how data moves through Apache Airflow DAGs, Spark transformations, dbt models, or Flink streams.

Enter **OpenLineage**, the Linux Foundation (LF AI & Data) open standard for operational lineage metadata, and **Marquez**, its reference metadata collection and lineage visualization engine.

In this walkthrough, we examine how we built an **OpenLineage and Marquez data-source connector for Cognee** ([`topoteretes/cognee#5554`](https://github.com/topoteretes/cognee/issues/5554)), enabling AI agents to ingest pipeline topologies, execution runs, and dataset schemas directly into cognitive memory graphs.

---

## 2. Why Cognee?

[Cognee](https://github.com/topoteretes/cognee) is the open-source memory engine for AI agents. Rather than dumping raw unstructured text into basic vector stores:

1. Cognee extracts structured entities and semantic relationships from incoming documents via its `cognify` pipeline.
2. It constructs interconnected Knowledge Graphs alongside vector embeddings.
3. It enables Graph Completion search (`cognee.search(..., query_type=SearchType.GRAPH_COMPLETION)`), allowing agents to traverse relationships across jobs, datasets, runs, and schema facets.

A connector in Cognee bridges upstream systems into this shared cognitive architecture.

![Cognee OpenLineage Architecture](https://raw.githubusercontent.com/somuai/cognee-community/feat/connector-openlineage/packages/connector/openlineage/assets/openlineage_memory_architecture.png)

---

## 3. Connector Architecture: Document-Mode Routing

When designing the OpenLineage connector under `packages/connector/openlineage/`, we established three architectural principles:

### A. Semantic Lineage Projections (Not Raw RunEvent Telemetry)

An enterprise OpenLineage backend receives millions of low-level JSON `RunEvent` objects containing transient transport data. Ingesting raw JSON into an LLM produces token bloat and degraded retrieval quality.

Instead, the connector queries Marquez REST API endpoints (`/api/v1/namespaces`, `/api/v1/jobs`, `/api/v1/runs`, `/api/v1/datasets`) and synthesizes high-density **architectural Markdown prose**:
- **Job Definitions**: Namespaces, job names, job types (`BATCH`, `STREAMING`, `SERVICE`), and descriptions.
- **Input & Output Datasets**: Explicit upstream sources and downstream targets, including dataset physical locations.
- **Schema Facets**: Column names, data types, nullability, and documentation.
- **Execution Run Histories**: Nominal start times, completion timestamps, durations, and captured error messages on failed runs.

### B. Cognee Document-Mode Routing

Tabular data in `dlt` is typically treated as rigid relational tables. Lineage, however, is richest when modeled as structural documentation.

By setting the Cognee document-mode marker:
```python
from cognee.tasks.ingestion.dlt_utils import DOCUMENT_SOURCE_ATTR

source = _openlineage()
setattr(source, DOCUMENT_SOURCE_ATTR, "openlineage")
```
Cognee's ingestion pipeline routes each job and its dependencies into the full `cognify` entity-extraction engine. The LLM extracts explicit graph nodes (`Job`, `Dataset`, `Schema`, `Run`) and relationships (`READS_FROM`, `WRITES_TO`, `HAS_RUN`, `PRODUCES`), storing them in graph memory.

---

## 4. Production Reliability: Full-Snapshot Sync and Forget-on-Delete

In production data stacks, pipeline DAGs are continuously renamed, refactored, or deprecated. A major failure mode in AI memory is **stale ghost entities**: an agent hallucinating that a pipeline job exists weeks after it was decommissioned.

To resolve this, the connector enforces a **full-snapshot replacement strategy**:

![OpenLineage Connector Core Implementation Snippet](https://raw.githubusercontent.com/somuai/cognee-community/feat/connector-openlineage/packages/connector/openlineage/assets/openlineage_connector_snippet.png)

### How Forget-on-Delete Works:

1. `write_disposition="replace"` ensures each sync run replaces staging with the exact set of jobs visible in the catalog.
2. If an Airflow DAG or Spark job is decommissioned upstream, it drops out of the Marquez catalog listing.
3. On the next sync run, the job is absent from staging.
4. Cognee's internal `orphan_cleanup` reconciles the missing entity out of the knowledge graph and vector indices.
5. Unchanged jobs retain stable content hashes (`data_id`), ensuring they are **not re-cognified**, saving LLM token costs.
6. Sensitive values matching `token`, `secret`, `password`, or `key` are defensively redacted prior to ingestion.

---

## 5. Live Execution & Verification

All connector code was validated with end-to-end tests covering namespace discovery, facet parsing, secret redaction, and snapshot replacement:

![Terminal Execution & Verification](https://raw.githubusercontent.com/somuai/cognee-community/feat/connector-openlineage/packages/connector/openlineage/assets/openlineage_terminal_verification.png)

### Execution Highlights:
- **Linting & Formatting**: 100% clean under `ruff check` and `ruff format`.
- **Test Suite**: 10 unit and integration tests passing deterministically offline using an in-memory `FakeMarquezClient`.
- **Zero Cloud Leakage**: Zero external network requests required during CI validation.

---

## 6. Querying Lineage Memory with Graph Completion

Using the connector in an autonomous agent application requires only a few lines of code:

```python
import asyncio
import os
import cognee
from cognee_community_connector_openlineage import openlineage_source

DATASET_NAME = "pipeline_lineage_memory"

async def main():
    # 1. Configure the OpenLineage source pointing to Marquez
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

    # 3. Ask cross-pipeline structural questions
    query = (
        "Which upstream jobs populate the retention_metrics dataset, "
        "and did any recent runs fail?"
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

When the agent queries lineage memory, it traverses the knowledge graph to synthesize a precise root-cause analysis:

![Agent Lineage Query Input and Output](https://raw.githubusercontent.com/somuai/cognee-community/feat/connector-openlineage/packages/connector/openlineage/assets/agent_query_openlineage_lineage.png)

Notice that the agent did not need to sift through thousands of raw JSON log events. It traversed the graph directly from dataset to job to failed run, immediately surfacing the exact error facet (`OutOfMemoryError: Java heap space`).

---

## 7. Reliability and Testing in CI

To ensure frictionless maintainer review and rock-solid CI pipelines:
- The entire test suite (`tests/test_openlineage.py`) runs offline against `FakeMarquezClient`.
- Tests verify:
  1. Markdown table formatting for column IDs, types, and docstrings.
  2. Dataset schema facet extraction across inputs and outputs.
  3. Transient vs. permanent HTTP error classification (exponential backoff retry on 500/503; 404 drops gracefully).
  4. Defensive redaction of sensitive properties and API tokens.
  5. Full-snapshot reconciliation: verifying that dropping a job upstream removes it from staging on the subsequent run.

---

## 8. Summary & Key Takeaways

By bridging OpenLineage and Marquez into Cognee:
- **Lineage Awareness**: AI agents gain persistent, self-updating awareness of end-to-end data pipeline topologies.
- **Zero Token Waste**: Ingests architectural projections rather than dumping millions of raw JSON RunEvents.
- **Zero Ghost Entities**: Decommissioned pipelines are automatically pruned from memory via full-snapshot reconciliation.
- **Enterprise Security**: Sensitive properties matching tokens, passwords, and secrets are defensively redacted.

The code is available in fork branch [`somuai/cognee-community:feat/connector-openlineage`](https://github.com/somuai/cognee-community/tree/feat/connector-openlineage) under `packages/connector/openlineage/`, resolving [`topoteretes/cognee#5554`](https://github.com/topoteretes/cognee/issues/5554).
