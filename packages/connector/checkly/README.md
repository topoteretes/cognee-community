# cognee-community-connector-checkly

A Checkly synthetic API and browser monitoring data-source connector for [cognee](https://github.com/topoteretes/cognee):
sync your team's synthetic checks, check groups, alert channels, and maintenance windows into cognee memory — "ask my monitoring stack".

It exposes a `dlt` resource you pass directly to `cognee.remember(...)`, integrating seamlessly with cognee's DLT ingestion pipeline (`resolve_dlt_sources` → `ingest_dlt_source` → `orphan_cleanup`). This provides:
- **Incremental synchronization**: only checks, groups, or alert channels whose configuration or execution health status changed are re-indexed.
- **Forget-on-delete**: resources deleted in Checkly (or that fall out of tag filters) are automatically purged from memory on subsequent syncs.
- **Zero credential leakage**: synthetic checks often include sensitive `Authorization` headers, cookies, or environment variables. The connector strictly redacts all header values and environment variable contents, outputting only keys (`Headers (keys only)` and `Environment Variables (keys only)`).

## Requirements

- Python 3.10 to 3.14.
- `cognee>=1.6.3,<2` (for table-scoped document cleanup and `PIPELINE_SCOPE_ATTR`).
- A Checkly account and API key.

## Installation

```bash
uv pip install cognee-community-connector-checkly
# or from this repository:
cd packages/connector/checkly && uv sync
```

## Quickstart

```python
import cognee
from cognee_community_connector_checkly import checkly_source

# Sync synthetic checks, groups, and alert channels into cognee
await cognee.remember(
    checkly_source(
        resource_name="checkly_prod",
        api_key="cu_...",
        account_id="acc_...",  # Optional: required only for multi-account users
        include=("checks", "check_groups", "alert_channels", "maintenance_windows"),
    ),
    dataset_name="checkly_synthetic_monitoring",
    primary_key="id",
    write_disposition="merge",
    max_rows_per_table=0,
)

# Search your monitoring knowledge graph
answer = await cognee.search(
    query_text="Which synthetic API checks are failing or degraded, and which alert channels notify on failure?",
    query_type=cognee.SearchType.GRAPH_COMPLETION,
    datasets=["checkly_synthetic_monitoring"],
)
print(answer)
```

> **Note**: `write_disposition="merge"` is required so incremental runs upsert by record ID rather than truncating existing tables.

## What Gets Ingested

- **Checks**: Name, check type (API, BROWSER, MULTI_STEP), target URL/script summary, frequency, locations, parent group, current health status (`HEALTHY`, `DEGRADED`, `FAILING`), tags, header keys (values redacted), and web dashboard URL.
- **Check Groups**: Group name, state (active/paused), concurrency, locations, tags, environment variable keys (values redacted), and web dashboard URL.
- **Alert Channels**: Channel name, channel type (SLACK, WEBHOOK, EMAIL, PAGERDUTY, SMS), failure/recovery alert toggles, and web dashboard URL.
- **Maintenance Windows**: Window name, starts at, ends at, repeat unit, tags, and web dashboard URL.

## Security & Credential Redaction

Synthetic checks commonly make authenticated HTTP requests using production bearer tokens, basic auth passwords, or API keys. To ensure zero credential leakage:
- Header values in check requests are **strictly redacted** (only header keys are indexed).
- Environment variable values in check groups are **strictly redacted** (only variable names are indexed).
- Webhook endpoints and channel configurations are **redacted** (`[Configured & Protected]`).
- Client representations redact authentication tokens (`ChecklyClient(base_url=..., api_key=<redacted>)`).

## How Incremental Sync & Forget-on-Delete Work

1. **Deterministic Fingerprints**: Each record is fingerprinted via SHA-256 over its normalized representation and current status. On subsequent sync runs, records with identical fingerprints are skipped (`stats["skipped"]`).
2. **Health Status Change Detection**: Check representations incorporate current status from Checkly's status API. When a check degrades or fails, its fingerprint changes and the updated record is re-emitted into memory.
3. **Forget-on-Delete**: When a complete sync cycle finishes, any IDs previously tracked but no longer returned are checked via individual GET endpoints. If the object returns HTTP 404 or was filtered out, a tombstone `{"id": row_id, "_deleted": True}` is emitted. DLT drops the row and cognee's `orphan_cleanup` purges it from relational, graph, and vector stores.

## Testing

The test suite runs 100% offline with zero external network dependencies:

```bash
pytest packages/connector/checkly/tests
```
