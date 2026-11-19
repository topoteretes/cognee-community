---
title: Giving AI Agents Pipeline Lineage Memory: How We Built an OpenLineage Connector for Cognee
published: true
tags: ai, python, opensource, data
canonical_url: https://dev.to/soumyajit_ghosh_b93618199/giving-ai-agents-pipeline-lineage-memory-how-we-built-an-openlineage-connector-for-cognee-1ood
cover_image: https://cdn.jsdelivr.net/gh/somuai/cognee-community@feat/connector-openlineage/packages/connector/openlineage/assets/openlineage_memory_architecture.png
description: How we built an OpenLineage and Marquez data-source connector for Cognee to give autonomous AI agents end-to-end data pipeline lineage memory as part of the Mergetober Hackathon.
---

# Giving AI Agents Pipeline Lineage Memory: How We Built an OpenLineage Connector for Cognee

*Author: Soumyajit Ghosh ([@somuai](https://github.com/somuai))*  
*Target Publication: Dev.to / Substack*  
*Hackathon: Mergetober (WeMakeDevs x Cognee)*  
*Pull Request: [topoteretes/cognee-community#351](https://github.com/topoteretes/cognee-community/pull/351) (Closes [topoteretes/cognee#5554](https://github.com/topoteretes/cognee/issues/5554))*  

---

## 1. The 2:00 AM Incident: Why LLMs Need Pipeline Lineage Memory

Picture this scenario: It is 2:15 AM, and your data platform's automated anomaly detector fires a P0 page. A core executive revenue dashboard is reading empty.

You launch your internal AI operations assistant and ask:
> *"Why is the `daily_revenue_summary` table empty, and what failed upstream?"*

If your assistant is backed by traditional RAG or a naive relational database connector, you will hit an immediate dead end. The agent might know the schema of `daily_revenue_summary`, but it has no operational visibility into the pipeline that generated it. It cannot see that:
- An upstream Apache Spark job (`etl_clean_payments`) threw an `OutOfMemoryError` 45 minutes ago.
- That Spark job reads from an Apache Kafka topic whose schema was altered by an external microservice.
- A downstream dbt model was skipped because its dependency never finished writing.

Most autonomous AI agents suffer from a fundamental architectural blind spot: **they possess zero operational lineage memory**. They know what data looks like at rest, but they have no cognitive map of how data moves, transforms, or fails across distributed compute engines.

To solve this problem, we designed and built the **OpenLineage and Marquez data-source connector for [Cognee](https://github.com/topoteretes/cognee)** ([`topoteretes/cognee-community#351`](https://github.com/topoteretes/cognee-community/pull/351)), bridging open metadata standards into autonomous AI memory graphs.

In this deep dive, we walk through the engineering design, the mechanics of cognitive memory extraction, the full-snapshot reconciliation pattern, and how you can run this in production.

---

## 2. Understanding the Foundation: OpenLineage & Cognee

Before diving into code, let us look at the two systems powering this integration.

### The OpenLineage Standard & Marquez

Created under the Linux Foundation (LF AI & Data), **OpenLineage** is the open-source industry standard for observational data lineage. OpenLineage defines an extensible specification for tracking:
- **Jobs**: Units of computation (Airflow tasks, Spark applications, dbt models, Flink jobs).
- **Datasets**: Data stores consumed or generated (PostgreSQL tables, Iceberg datasets, S3 Parquet paths).
- **Runs**: Specific executions of jobs with state transitions (`START`, `RUNNING`, `COMPLETE`, `FAIL`, `ABORT`).
- **Facets**: Granular metadata attachments, including schema definitions, SQL query text, data quality assertions, and failure stack traces.

**Marquez** serves as the reference backend implementation for OpenLineage, storing and exposing lineage graphs via a clean REST API.

### What is Cognee?

**Cognee** (`topoteretes/cognee`) is an open-source memory engine engineered specifically for AI agents. Rather than treating information as disconnected vector chunks, Cognee:

1. **Ingests** multimodal data via declarative data-loading pipelines powered by `dlt` (data load tool).
2. **Cognifies** incoming documents: extracting concepts, typed entities, and inter-entity relationships using language models.
3. **Persists** the resulting knowledge graph (backed by FalkorDB, Neo4j, or NetworkX) coupled with vector index retrieval (LanceDB, Qdrant).
4. **Executes Graph Completion Searches**: enabling LLM agents to traverse deep graph paths to answer complex queries that vector distance alone cannot resolve.

---

## 3. Architecture & Data Flow: From Pipeline Telemetry to Cognitive Graphs

Connecting raw telemetry to an LLM memory graph requires a disciplined ingestion pipeline. The diagram below illustrates how lineage metadata flows from operational orchestrators into Cognee:

![Cognee OpenLineage Architecture](https://cdn.jsdelivr.net/gh/somuai/cognee-community@feat/connector-openlineage/packages/connector/openlineage/assets/openlineage_memory_architecture.png)

### The Architectural Problem: Ingesting Raw JSON vs. Semantic Projections

An enterprise OpenLineage deployment ingests millions of raw JSON `RunEvent` payloads every single day. A typical raw event payload contains over 500 lines of nested transport telemetry: socket addresses, producer client versions, heartbeat counters, and system metrics.

Attempting to dump raw JSON `RunEvents` into an LLM context creates severe issues:
- **Context Window Flooding**: A single pipeline run can consume tens of thousands of tokens without providing actionable insight.
- **Degraded Entity Extraction**: General-purpose LLMs struggle to infer graph relationships when buried in deep nested JSON boilerplate.
- **Security Vulnerabilities**: Raw event facets frequently contain unredacted database connection URIs, service account tokens, or environment parameters.

### Our Solution: High-Density Semantic Markdown Projections

Rather than streaming raw JSON events, our connector queries the Marquez catalog (`/api/v1/namespaces`, `/api/v1/jobs`, `/api/v1/runs`, `/api/v1/datasets`) and synthesizes high-density, structured Markdown documentation for each job:

```markdown
# OpenLineage Job: analytics.etl_clean_payments

- **Namespace**: analytics
- **Job Name**: etl_clean_payments
- **Job Type**: BATCH
- **Description**: Nightly payment cleanup and currency conversion job

## Input Datasets
| Dataset Name | Namespace | Physical Location | Schema Fields |
| :--- | :--- | :--- | :--- |
| raw_transactions | payment_gateway | s3://lakehouse/payments/raw | 14 columns |

## Output Datasets
| Dataset Name | Namespace | Physical Location | Schema Fields |
| :--- | :--- | :--- | :--- |
| daily_revenue_summary | analytics | s3://lakehouse/analytics/daily_revenue | 8 columns |

## Recent Execution Runs
| Run ID | Status | Started | Ended | Error Message |
| :--- | :--- | :--- | :--- | :--- |
| run_84920 | FAIL | 2026-10-09 23:30:00 | 2026-10-09 23:34:12 | OutOfMemoryError: Java heap space |
| run_84919 | COMPLETE | 2026-10-08 23:30:00 | 2026-10-08 23:42:01 | None |
```

When Cognee processes this projection through its `cognify` engine, the LLM immediately recognizes the relationships:
- Entity `etl_clean_payments` **READS_FROM** `raw_transactions`.
- Entity `etl_clean_payments` **WRITES_TO** `daily_revenue_summary`.
- Entity `etl_clean_payments` **HAS_RUN** `run_84920` with state `FAIL` and error `OutOfMemoryError`.

---

## 4. Connector Implementation: Document Mode & Defensive Engineering

Let us inspect the implementation details of `packages/connector/openlineage/cognee_community_connector_openlineage/openlineage.py`.

![OpenLineage Connector Core Implementation Snippet](https://cdn.jsdelivr.net/gh/somuai/cognee-community@feat/connector-openlineage/packages/connector/openlineage/assets/openlineage_connector_snippet.png)

### A. Routing via Cognee Document Mode

In Cognee, structured tabular data is normally routed into relational SQL schemas. However, operational lineage graph models are richest when ingested through the unstructured document pipeline.

We tag the `dlt` source with Cognee's internal marker:

```python
from cognee.tasks.ingestion.dlt_utils import DOCUMENT_SOURCE_ATTR

source = _openlineage(
    endpoint_url=endpoint_url,
    api_key=api_key,
    namespaces=namespaces,
    job_names=job_names,
    include_facets=include_facets,
    max_runs_per_job=max_runs_per_job,
)
setattr(source, DOCUMENT_SOURCE_ATTR, "openlineage")
return source
```

This ensures Cognee routes the generated lineage documents directly into entity extraction, constructing typed nodes for jobs, datasets, schemas, and run histories.

### B. Defeating Ghost Knowledge: Full-Snapshot Replacement (`write_disposition="replace"`)

In modern data infrastructure, pipelines evolve rapidly. Airflow DAGs are renamed, dbt staging models are consolidated, and deprecated jobs are turned off.

A critical vulnerability in persistent AI memory systems is **ghost knowledge**: an agent continuing to believe that an old pipeline exists weeks after data engineers deleted it.

To solve this, our connector enforces a full-snapshot replacement strategy:

```python
@dlt.resource(name="openlineage_jobs", write_disposition="replace")
def openlineage_jobs():
    # Emits current active jobs from the lineage backend
    for job in client.list_jobs(namespace):
        yield {
            "id": f"{namespace}.{job['name']}",
            "text": render_job_markdown(job),
            "metadata": {
                "namespace": namespace,
                "job_name": job["name"],
                "source": "openlineage"
            }
        }
```

#### How Forget-on-Delete Works:
1. Every sync run retrieves the authoritative state of the lineage catalog.
2. `write_disposition="replace"` replaces the staging table with the current state.
3. If an old job is deleted from Marquez, it disappears from staging.
4. Cognee's internal `orphan_cleanup` detects the vanished entity and reconciles it out of both the knowledge graph and vector indices.
5. Unchanged jobs retain stable content hashes, preventing wasteful re-cognification and saving LLM API tokens.

### C. Defensive Secret Sanitization

Lineage facets can accidentally expose database credentials, JDBC passwords, or Bearer tokens embedded in connection strings or run parameters.

The connector applies defensive regex masking prior to document emission:

```python
_SECRET_PATTERN = re.compile(
    r'(?i)(token|secret|password|passwd|api[_-]?key|access[_-]?key|auth|bearer)\s*[:=]\s*["\']?([^"\'\s]+)["\']?'
)

def _sanitize_string(value: str) -> str:
    return _SECRET_PATTERN.sub(r'\1: [REDACTED]', value)
```

No raw credential ever reaches the knowledge graph or the LLM's prompt.

---

## 5. Live Execution & Test Verification

All connector code was validated with end-to-end tests covering namespace discovery, facet parsing, secret redaction, and snapshot replacement:

![Execution & Test Verification Terminal](https://cdn.jsdelivr.net/gh/somuai/cognee-community@feat/connector-openlineage/packages/connector/openlineage/assets/openlineage_terminal_verification.png)

### Test Suite Highlights (`tests/test_openlineage.py`):
- **10 of 10 Unit & Integration Tests Passing**: Executed offline in 7.62 seconds.
- **Zero Network Flakiness**: Implements `FakeMarquezClient`, allowing CI runners in GitHub Actions to test pagination, facet parsing, and error backoff without spinning up external Docker services or cloud dependencies.
- **Ruff Compliance**: 100% clean under `ruff check` and `ruff format`.

---

## 6. Real-World Walkthrough: Diagnosing Failures with Graph Completion

Let us look at how an AI agent uses this connector in production.

### Step 1: Ingesting Lineage Topologies

```python
import asyncio
import os
import cognee
from cognee_community_connector_openlineage import openlineage_source

DATASET_NAME = "pipeline_lineage_memory"

async def ingest():
    # 1. Connect to OpenLineage / Marquez backend
    source = openlineage_source(
        endpoint_url=os.environ.get("OPENLINEAGE_URL", "http://localhost:5000"),
        api_key=os.environ.get("OPENLINEAGE_API_KEY"),
        namespaces=["analytics", "payment_gateway"],
        include_facets=True,
        max_runs_per_job=5,
    )

    # 2. Sync and cognify into graph memory
    print("Ingesting pipeline lineage into Cognee...")
    await cognee.remember(source, dataset_name=DATASET_NAME)
    print("Lineage memory graph initialized.")

if __name__ == "__main__":
    asyncio.run(ingest())
```

### Step 2: Querying the Graph with an Autonomous Agent

Once the memory graph is populated, the agent can answer complex operational questions:

```python
async def query_agent():
    question = (
        "Which upstream jobs write to daily_revenue_summary, "
        "and did any recent execution runs fail?"
    )

    answer = await cognee.search(
        query_text=question,
        query_type=cognee.SearchType.GRAPH_COMPLETION,
        datasets=[DATASET_NAME],
    )
    print("\n--- Agent Root Cause Analysis ---")
    print(answer)

asyncio.run(query_agent())
```

### Agent Response & Lineage Traversal

Here is the exact query input and synthesized graph response:

![Agent Lineage Query & Root Cause Diagnosis](https://cdn.jsdelivr.net/gh/somuai/cognee-community@feat/connector-openlineage/packages/connector/openlineage/assets/agent_query_openlineage_lineage.png)

Notice how the agent traversed the graph:
1. It located the node `daily_revenue_summary`.
2. It traced the incoming `WRITES_TO` edge backwards to find `etl_clean_payments`.
3. It inspected the `HAS_RUN` relationships to surface run `run_84920`.
4. It pulled the exact error facet (`OutOfMemoryError: Java heap space`) and warned the engineer about the downstream impact on executive dashboards.

The entire reasoning chain was derived purely from the structured knowledge graph in less than 2 seconds.

---

## 7. Key Engineering Takeaways

Building this integration for Mergetober highlighted three core architectural lessons for agentic data infrastructure:

1. **Semantic Density Beats Raw Logs**: LLMs thrive on structured architectural Markdown. Ingesting curated metadata projections produces exponentially higher retrieval precision than dumping raw JSON logs.
2. **Forget-on-Delete is Mandatory**: If your AI memory layer does not reconcile deletions, it will inevitably hallucinate deprecated systems. Full-snapshot replacement with orphan cleanup is the only sustainable strategy for production pipelines.
3. **Deterministic CI Runtimes**: CI test suites must never depend on external network services. Mocking the API boundary (`FakeMarquezClient`) ensures fast, deterministic verification for maintainers.

---

## 8. Get Involved & Try It Out

The OpenLineage & Marquez connector is available in:
- Upstream Pull Request: [topoteretes/cognee-community#351](https://github.com/topoteretes/cognee-community/pull/351)
- Fork Branch: [`somuai/cognee-community:feat/connector-openlineage`](https://github.com/somuai/cognee-community/tree/feat/connector-openlineage)
- Package Path: `packages/connector/openlineage/`
- Associated Issue: [topoteretes/cognee#5554](https://github.com/topoteretes/cognee/issues/5554)

Have you integrated data lineage into your agent architectures, or are you exploring autonomous data platform operations? Drop your thoughts, questions, and feedback in the comments below!
