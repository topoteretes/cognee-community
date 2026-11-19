# cognee-community-connector-uptimerobot

An UptimeRobot website uptime and monitor status data-source connector for [cognee](https://github.com/topoteretes/cognee):
sync your team's website monitors, public status pages, alert contacts, and maintenance windows into cognee memory — "ask my uptime stack".

It exposes a `dlt` resource you hand directly to `cognee.remember(...)`, integrating seamlessly with cognee's DLT ingestion pipeline (`resolve_dlt_sources` → `ingest_dlt_source` → `orphan_cleanup`). This provides:
- **Incremental synchronization**: only monitors whose status (UP/DOWN/PAUSED), response times, or configurations changed are re-indexed.
- **Forget-on-delete**: monitors deleted in UptimeRobot are automatically purged from memory on subsequent syncs.
- **Zero credential leakage**: basic auth passwords and private webhook endpoints in alert contacts are strictly redacted, keeping credentials safe from vector embeddings and the knowledge graph.

## Requirements

- Python 3.10 to 3.14.
- `cognee>=1.6.3,<2` (for table-scoped document cleanup and `PIPELINE_SCOPE_ATTR`).
- An UptimeRobot account and API key.

## Installation

```bash
uv pip install cognee-community-connector-uptimerobot
# or from this repository:
cd packages/connector/uptimerobot && uv sync
```

## Quickstart

```python
import cognee
from cognee_community_connector_uptimerobot import uptimerobot_source

# Sync monitors, public status pages, and alert contacts into cognee
await cognee.remember(
    uptimerobot_source(
        resource_name="uptimerobot_prod",
        api_key="u123456-...",
        include=("monitors", "public_status_pages", "alert_contacts", "maintenance_windows"),
    ),
    dataset_name="uptime_monitoring",
    primary_key="id",
    write_disposition="merge",
    max_rows_per_table=0,
)

# Search your uptime knowledge graph
answer = await cognee.search(
    query_text="Which production monitors are DOWN or degraded, and what are their response times?",
    query_type=cognee.SearchType.GRAPH_COMPLETION,
    datasets=["uptime_monitoring"],
)
print(answer)
```

> **Note**: `write_disposition="merge"` is required so incremental runs upsert by record ID rather than truncating existing tables.

## What Gets Ingested

- **Monitors**: Friendly name, target URL, monitor type (HTTP, Keyword, Ping, Port), check interval, current status (`UP`, `DOWN`, `PAUSED`, `SEEMS_DOWN`), 24h/7d/30d uptime ratios, average response time, and web dashboard URL.
- **Public Status Pages**: Page name, status (active/paused), custom domain / standard URL, and assigned monitors count.
- **Alert Contacts**: Contact name, channel type (Slack, Email, Webhook, SMS, Twitter, PagerDuty), status, and protected destination placeholder.
- **Maintenance Windows**: Window name, duration, start time, and status.

## Security & Credential Redaction

UptimeRobot monitors can be configured with basic HTTP authentication, and alert contacts store sensitive webhook URLs. To ensure zero credential leakage:
- HTTP basic authentication passwords are **strictly redacted** (`[Redacted & Protected]`).
- Webhook URLs and contact endpoints are **strictly redacted** (`[Configured & Protected]`).
- Client representations redact authentication tokens (`UptimeRobotClient(base_url=..., api_key=<redacted>)`).

## How Incremental Sync & Forget-on-Delete Work

1. **Deterministic Fingerprints**: Each record is fingerprinted via SHA-256 over its normalized representation, operational status, and latency. On subsequent sync runs, records with identical fingerprints are skipped (`stats["skipped"]`).
2. **Uptime State Transition Detection**: When an endpoint goes `DOWN` or recovers to `UP`, its status shifts the SHA-256 fingerprint, prompting Cognee to immediately re-index the updated state.
3. **Forget-on-Delete**: When a complete sync cycle finishes, any IDs previously tracked but no longer returned are checked via individual `/getMonitors` lookups. If the object returns not found or failed status, a tombstone `{"id": row_id, "_deleted": True}` is emitted. DLT drops the row and cognee's `orphan_cleanup` purges it from relational, graph, and vector stores.

## Testing

The test suite runs 100% offline with zero external network dependencies:

```bash
pytest packages/connector/uptimerobot/tests
```
