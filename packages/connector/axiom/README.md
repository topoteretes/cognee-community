# cognee-community-connector-axiom

An Axiom cloud observability data-source connector for [cognee](https://github.com/topoteretes/cognee):
sync your datasets, monitors, alert rules, and notification channels into memory — "ask my observability stack".

It exposes a `dlt` resource you hand directly to `cognee.remember(...)`, utilizing cognee's native DLT
ingestion pipeline (`resolve_dlt_sources` → `ingest_dlt_source` → `orphan_cleanup`) — providing
**incremental re-sync** and **forget-on-delete** with zero core changes.

## Features

- **Datasets**: dataset names, descriptions, retention windows, and creation metadata.
- **Monitors & Alerts**: monitor names, alert status (`Active` vs. `Disabled`), interval cadence, thresholds, and full APL (Axiom Processing Language) query definitions.
- **Notifiers**: notification endpoints and integrations (Slack, PagerDuty, Webhooks, Email).
- **Incremental Resync**: Deterministic SHA-256 fingerprinting ensures unchanged datasets and monitors are skipped on subsequent syncs.
- **Forget on Delete**: Decommissioned monitors or dropped datasets produce tombstone records (`_deleted: True`) so cognee prunes orphaned memory nodes automatically.
- **Credential Sanitization**: Automatically scrubs Axiom personal/API tokens (`xapt-...`), Bearer tokens, and sensitive headers from outputs before feeding into LLM memory.

## Requirements

- Python 3.10 to 3.14.
- `cognee>=1.6.3,<2` for table-scoped document cleanup.
- An Axiom API Token or Personal Token (`xapt-...`).

## Install

```bash
uv pip install cognee-community-connector-axiom
# or, from this monorepo:
cd packages/connector/axiom && uv sync
```

## Usage

```python
import cognee
from cognee_community_connector_axiom import axiom_source

await cognee.remember(
    axiom_source(
        api_key="xapt-...",
        resource_name="axiom_prod",
    ),
    dataset_name="observability_memory",
    primary_key="id",
    write_disposition="merge",  # incremental upsert by record id
    max_rows_per_table=0,
)

answer = await cognee.search(
    query_text="Which monitors are currently configured for HTTP 5xx spikes, and what is their APL query?",
    query_type=cognee.SearchType.GRAPH_COMPLETION,
    datasets=["observability_memory"],
)
```

## Options

- `include`: tuple of kinds to ingest, default `("datasets", "monitors", "notifiers")`.
- `api_url`: custom Axiom API URL (default `https://api.axiom.co/v1`).
- `dashboard_url`: web dashboard base URL for document deep-links (default `https://app.axiom.co`).
- `max_requests`: request budget per run (default `500`).
- `check_active`: optional callback checkpoint invoked before each request.
