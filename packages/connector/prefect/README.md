# cognee-community-connector-prefect

A Prefect workflow orchestration data-source connector for [cognee](https://github.com/topoteretes/cognee):
sync your team's flows, deployments, schedules, and flow run execution history into memory — "ask my orchestrator".

It exposes a `dlt` resource you hand directly to `cognee.remember(...)`, integrating seamlessly with cognee's DLT ingestion engine (`resolve_dlt_sources` → `ingest_dlt_source` → `orphan_cleanup`). This provides:
- **Incremental synchronization**: only flows, deployments, or runs whose content or execution state changed are re-indexed.
- **Forget-on-delete**: resources deleted in Prefect (or that fall out of tag/state filters) are automatically purged from memory on subsequent syncs.
- **Zero parameter secret leakage**: Prefect deployment and flow run parameters often store sensitive database credentials or API keys. The connector strictly audits parameters to output only parameter keys (`Parameters (keys only)`), guaranteeing credentials never enter the knowledge graph or vector embeddings.

## Requirements

- Python 3.10 to 3.14.
- `cognee>=1.6.3,<2` (for table-scoped document cleanup and `PIPELINE_SCOPE_ATTR`).
- Prefect 2.x or 3.x (Self-Hosted server or Prefect Cloud).

## Installation

```bash
uv pip install cognee-community-connector-prefect
# or from this repository:
cd packages/connector/prefect && uv sync
```

## Quickstart

```python
import cognee
from cognee_community_connector_prefect import prefect_source

# Sync flows, deployments, and flow runs into cognee
await cognee.remember(
    prefect_source(
        resource_name="prefect_prod",
        base_url="https://api.prefect.cloud/api/accounts/<account-id>/workspaces/<workspace-id>",
        api_key="pnu_...",  # Omit for unauthenticated self-hosted servers
        include=("flows", "deployments", "flow_runs"),
    ),
    dataset_name="prefect_orchestration",
    primary_key="id",
    write_disposition="merge",
    max_rows_per_table=0,
)

# Search your orchestration knowledge graph
answer = await cognee.search(
    query_text="Which deployments are currently active and what runs failed recently?",
    query_type=cognee.SearchType.GRAPH_COMPLETION,
    datasets=["prefect_orchestration"],
)
print(answer)
```

> **Note**: `write_disposition="merge"` is required so incremental updates upsert by record ID rather than truncating existing tables.

## What Gets Ingested

- **Flows**: Name, description, tags, creation and update timestamps, and web UI URL.
- **Deployments**: Name, associated flow name, status (active/paused), schedules (cron, interval, rrule), work pool, work queue, entrypoint, tags, parameter keys (values redacted), and web UI URL.
- **Flow Runs**: Run name, parent flow name, parent deployment name, execution state (`COMPLETED`, `FAILED`, `RUNNING`, `CRASHED`, etc.), start time, end time, duration, run count, tags, parameter keys (values redacted), and web UI URL.

## Security & Parameter Redaction

Prefect flows and deployments routinely receive operational parameters such as database connection strings, passwords, or internal API tokens. To protect production secrets:
- Parameter values are **never** rendered into the emitted document content or URLs.
- Only parameter names/keys are extracted (e.g. `Parameters (keys only): batch_size, target_table`), providing structural context without compromising security.
- Client representations redact authentication tokens (`PrefectClient(base_url=..., api_key=<redacted>)`).

## How Incremental Sync & Forget-on-Delete Work

1. **Deterministic Fingerprints**: Each record is fingerprinted via SHA-256 over its normalized representation. On subsequent sync runs, records with identical fingerprints are skipped (`stats["skipped"]`).
2. **State Transition Detection**: Flow run fingerprints incorporate state names, run times, and completion timestamps. When an in-flight run transitions from `RUNNING` to `COMPLETED` or `FAILED`, its fingerprint updates and the new record is emitted.
3. **Forget-on-Delete**: When a complete sync cycle finishes, any IDs previously tracked but no longer returned are checked via individual GET endpoints. If the object returns HTTP 404 or was filtered out, a tombstone `{"id": row_id, "_deleted": True}` is emitted. DLT drops the row and cognee's `orphan_cleanup` purges it from relational, graph, and vector stores.

## Testing

The test suite runs 100% offline with zero external network dependencies:

```bash
pytest packages/connector/prefect/tests
```
