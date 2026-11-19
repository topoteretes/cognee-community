# Kestra Orchestration Platform Connector for Cognee

A `dlt` data-source connector that ingests flow definitions, execution states, task run durations, and operational failure logs from Kestra into Cognee memory for automated DevOps troubleshooting and root cause analysis.

Part of the **Cognee Mergetober Hackathon** (`#6171`).

---

## Features

- **Flow Topology & Metadata:** Ingests flow definitions, namespaces, child task dependencies, triggers, schedules, and labels.
- **Execution States & Telemetry:** Ingests execution histories with start/end timestamps, run durations, and per-task success/failure statuses.
- **Targeted Log Capture:** Pulls failure and warning logs directly into the execution document to enable immediate root-cause extraction.
- **Snapshot Replacement:** Uses `write_disposition="replace"` for clean synchronization with orphan cleanup.
- **Document Mode:** Tags resources with `DOCUMENT_SOURCE_ATTR` to route directly through Cognee's `cognify` entity-extraction pipeline.

---

## Quickstart

```python
import asyncio
import os
import cognee
from cognee_community_connector_kestra import kestra_source

async def main():
    # 1. Initialize Kestra connector
    source = kestra_source(
        base_url=os.getenv("KESTRA_BASE_URL", "http://localhost:8080"),
        token=os.getenv("KESTRA_TOKEN"),
        namespaces=["prod", "etl"],
        include_logs=True,
    )

    # 2. Add to Cognee memory
    await cognee.add(source)

    # 3. Cognify into knowledge graph
    await cognee.cognify()

    # 4. Search operational memory
    results = await cognee.search("Why did the nightly ingest flow fail last week?")
    for res in results:
        print("-", res)

if __name__ == "__main__":
    asyncio.run(main())
```

---

## Running Tests

Run the hermetic offline test suite:

```bash
pytest packages/connector/kestra/tests
```
