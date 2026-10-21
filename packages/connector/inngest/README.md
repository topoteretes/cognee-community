# cognee-community-connector-inngest

An Inngest data-source connector for [cognee](https://github.com/topoteretes/cognee):
sync your durable workflow functions, execution run histories, failure stacktraces, and event triggers into memory — "ask my background workflows".

It exposes a `dlt` resource you hand directly to `cognee.remember(...)`, utilizing cognee's native DLT
ingestion pipeline (`resolve_dlt_sources` → `ingest_dlt_source` → `orphan_cleanup`) — providing
**incremental re-sync** and **forget-on-delete** with zero core changes.

## Features

- **Durable Functions**: function names, IDs, triggers (event triggers, cron schedules), and concurrency limits.
- **Workflow Execution Runs**: run statuses (`COMPLETED`, `FAILED`, `CANCELLED`), start/end timestamps, output previews, and error stacktraces.
- **Event Catalog**: event triggers, timestamps, and schema names.
- **Incremental Resync**: SHA-256 fingerprinting ensures unchanged functions and historical runs are skipped on subsequent runs.
- **Forget on Delete**: Decommissioned functions or events produce tombstone records (`_deleted: True`) so cognee prunes orphaned memory nodes automatically.
- **Credential Sanitization**: Automatically scrubs signing keys (`signkey-prod-...`, `signkey-test-...`), Bearer tokens, and sensitive headers from outputs and stacktraces before feeding into LLM memory.

## Requirements

- Python 3.10 to 3.14.
- `cognee>=1.6.3,<2` for table-scoped document cleanup.
- An Inngest Signing Key or Management API Key (`signkey-prod-...`).

## Install

```bash
uv pip install cognee-community-connector-inngest
# or, from this monorepo:
cd packages/connector/inngest && uv sync
```

## Usage

```python
import cognee
from cognee_community_connector_inngest import inngest_source

await cognee.remember(
    inngest_source(
        api_key="signkey-prod-...",
        resource_name="inngest_prod",
    ),
    dataset_name="workflows_memory",
    primary_key="id",
    write_disposition="merge",  # incremental upsert by record id
    max_rows_per_table=0,
)

answer = await cognee.search(
    query_text="Which functions failed in the last 24 hours and what was the root cause error in the stacktrace?",
    query_type=cognee.SearchType.GRAPH_COMPLETION,
    datasets=["workflows_memory"],
)
```

## Options

- `include`: tuple of kinds to ingest, default `("functions", "runs", "events")`.
- `api_url`: custom Inngest API URL (default `https://api.inngest.com/v1`).
- `dashboard_url`: web dashboard base URL for document deep-links (default `https://app.inngest.com`).
- `max_requests`: request budget per run (default `500`).
- `check_active`: optional callback checkpoint invoked before each request.
