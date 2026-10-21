# cognee-community-connector-flyio

A Fly.io infrastructure and deployment data-source connector for [cognee](https://github.com/topoteretes/cognee):
sync your global apps, machines, regions, and NVMe volumes into memory — "ask my global infrastructure".

It exposes a `dlt` resource you hand directly to `cognee.remember(...)`, utilizing cognee's native DLT
ingestion pipeline (`resolve_dlt_sources` → `ingest_dlt_source` → `orphan_cleanup`) — providing
**incremental re-sync** and **forget-on-delete** with zero core changes.

## Features

- **Global Apps**: app names, organizations, statuses (`deployed`, `suspended`), hostnames (`app.fly.dev`), and release metadata.
- **Machines**: machine IDs, global regions (`iad`, `fra`, `syd`), states (`started`, `stopped`), VM CPU/RAM sizing, image tags, and health checks.
- **Persistent Volumes**: volume IDs, regions, size (GB), attached machines, and encryption state.
- **Incremental Resync**: Deterministic SHA-256 fingerprinting ensures unchanged apps, machines, and volumes are skipped on subsequent runs.
- **Forget on Delete**: Destroyed machines, deleted volumes, or decommissioned apps emit tombstone records (`_deleted: True`) so cognee prunes orphaned memory nodes automatically.
- **Credential Sanitization**: Automatically scrubs Fly.io API tokens (`FlyV1 ...`, `fo1_...`), Bearer tokens, and sensitive env variables before feeding into LLM memory.

## Requirements

- Python 3.10 to 3.14.
- `cognee>=1.6.3,<2` for table-scoped document cleanup.
- A Fly.io API Token (`FlyV1 ...`) from `fly auth token` or the dashboard.

## Install

```bash
uv pip install cognee-community-connector-flyio
# or, from this monorepo:
cd packages/connector/flyio && uv sync
```

## Usage

```python
import cognee
from cognee_community_connector_flyio import flyio_source

await cognee.remember(
    flyio_source(
        api_key="FlyV1 ...",
        resource_name="flyio_prod",
    ),
    dataset_name="infrastructure_memory",
    primary_key="id",
    write_disposition="merge",  # incremental upsert by record id
    max_rows_per_table=0,
)

answer = await cognee.search(
    query_text="Which machines are running in region fra for app api-service, and are all health checks passing?",
    query_type=cognee.SearchType.GRAPH_COMPLETION,
    datasets=["infrastructure_memory"],
)
```

## Options

- `apps`: optional list of app names to scope ingestion.
- `org_slug`: optional organization slug to filter apps.
- `include`: tuple of kinds to ingest, default `("apps", "machines", "volumes")`.
- `api_url`: custom Fly.io Machines API URL (default `https://api.machines.dev/v1`).
- `dashboard_url`: web dashboard base URL for document deep-links (default `https://fly.io/dashboard`).
- `max_requests`: request budget per run (default `500`).
- `check_active`: optional callback checkpoint invoked before each request.
