# cognee-community-connector-doppler

A Doppler secrets and configuration data-source connector for [cognee](https://github.com/topoteretes/cognee):
sync your environments, project configurations, variable names, and activity audit logs into memory — "ask my infrastructure configs".

It exposes a `dlt` resource you hand directly to `cognee.remember(...)`, using cognee's existing DLT
ingestion pipeline (`resolve_dlt_sources` → `ingest_dlt_source` → `orphan_cleanup`) — providing
**incremental re-sync** and **forget-on-delete** with zero core changes.

## Security Guarantee: Zero Secret Leakage

> [!IMPORTANT]
> **Secret values are NEVER fetched, read, or stored.**
> This connector indexes configuration metadata, variable names (e.g., `DATABASE_URL`, `REDIS_HOST`),
> environment hierarchies, and activity audit logs. Your actual secret values remain securely in Doppler.

## Requirements

- Python 3.10 to 3.14.
- `cognee>=1.6.3,<2` for table-scoped document cleanup.
- A Doppler Service Token (`dp.st....`) or Personal Token (`dp.pt....`).

## Install

```bash
uv pip install cognee-community-connector-doppler
# or, from this monorepo:
cd packages/connector/doppler && uv sync
```

## Usage

```python
import cognee
from cognee_community_connector_doppler import doppler_source

await cognee.remember(
    doppler_source(
        api_key="dp.st.production_...",
        resource_name="doppler_main",
    ),
    dataset_name="devops_memory",
    primary_key="id",
    write_disposition="merge",  # incremental upsert by record id
    max_rows_per_table=0,
)

answer = await cognee.search(
    query_text="Which services have a REDIS_URL variable configured, and who changed the staging configs last week?",
    query_type=cognee.SearchType.GRAPH_COMPLETION,
    datasets=["devops_memory"],
)
```

## What gets ingested

- **Projects**: name, slug, description, and created timestamp.
- **Environments**: project name, environment name, and slug (`prd`, `stg`, `dev`).
- **Configs**: config name, project, environment, root status, and sorted list of configured variable keys.
- **Activity Logs**: audit log entry, actor, action description, config name, and timestamp.

## Options

- `projects`: optional list of project names to scope ingestion.
- `include`: tuple of kinds to ingest, default `("projects", "environments", "configs", "logs")`.
- `api_url`: custom Doppler API URL (default `https://api.doppler.com/v3`).
- `dashboard_url`: web dashboard base URL for document links (default `https://dashboard.doppler.com`).
- `max_requests`: request budget per run (default `500`).
- `check_active`: optional callback checkpoint invoked before each request.
