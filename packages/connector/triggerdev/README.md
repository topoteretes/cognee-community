# cognee-community-connector-triggerdev

A Trigger.dev data-source connector for [cognee](https://github.com/topoteretes/cognee):
sync your background tasks, execution runs, error logs, and cron schedules into memory — "ask my background jobs".

It exposes a `dlt` resource you hand directly to `cognee.remember(...)`, using cognee's existing DLT
ingestion pipeline (`resolve_dlt_sources` → `ingest_dlt_source` → `orphan_cleanup`) — so you get
**incremental re-sync** (only changed runs/tasks are re-processed) and **forget-on-delete**
(deleted tasks/schedules are purged from memory) with zero core changes.

## Requirements

- Python 3.10 to 3.14.
- `cognee>=1.6.3,<2` for table-scoped document cleanup.
- A Trigger.dev v3 account and Project API key (`tr_prod_...` or `tr_dev_...`).

## Install

```bash
uv pip install cognee-community-connector-triggerdev
# or, from this monorepo:
cd packages/connector/triggerdev && uv sync
```

## Usage

```python
import cognee
from cognee_community_connector_triggerdev import triggerdev_source

await cognee.remember(
    triggerdev_source(
        api_key="tr_prod_...",
        resource_name="triggerdev_prod",
    ),
    dataset_name="ops_memory",
    primary_key="id",
    write_disposition="merge",  # incremental upsert by record id
    max_rows_per_table=0,       # allow orphan cleanup across the synced corpus
)

answer = await cognee.search(
    query_text="Why did the stripe-webhook-handler task fail on the last execution?",
    query_type=cognee.SearchType.GRAPH_COMPLETION,
    datasets=["ops_memory"],
)
```

## What gets ingested

- **Tasks**: task slug, task identifier, file path, trigger source, created & updated timestamps.
- **Runs**: run ID, execution status (`COMPLETED`, `FAILED`, `CRASHED`, etc.), start & end times, duration, run number, tags, output summary, and detailed error stacktraces.
- **Schedules**: cron schedule ID, task identifier, cron expression, timezone, active status, last & next run timestamps.

## Security & PII Sanitization

- Sensitive API tokens, Authorization bearer headers, and credentials are automatically scrubbed and redacted from task outputs and error stacktraces before ingestion.

## Options

- `include`: tuple of kinds to ingest, default `("tasks", "runs", "schedules")`.
- `api_url`: custom Trigger.dev API endpoint for self-hosted instances (default `https://api.trigger.dev`).
- `dashboard_url`: web dashboard base URL for document links (default `https://cloud.trigger.dev`).
- `max_requests`: request budget per run (default `500`).
- `check_active`: optional callback checkpoint invoked before each request.
