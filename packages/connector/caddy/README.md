# cognee-community-connector-caddy

A **Caddy Web Server (`Caddyfile`)** reverse proxy & routing data-source connector for [cognee](https://github.com/topoteretes/cognee): turn your web server site blocks, reverse proxy destinations, path matchers, security headers, and TLS configurations into structured memory for AI DevOps agents.

It parses `Caddyfile` configurations and exposes a `dlt` resource you pass directly to `cognee.remember(...)`. It integrates cleanly with cognee's document ingestion pipeline, providing **snapshot sync** and **forget-on-delete** with zero core changes.

## Why This Matters for AI DevOps Agents

AI coding and DevOps assistants frequently hallucinate backend upstream ports, path routing rules (`handle_path`), security headers, and TLS email directives.

By ingesting `Caddyfile` into Cognee:
- Agents know the **exact reverse proxy upstreams and port mappings** (e.g., `127.0.0.1:8080`, `127.0.0.1:3000`).
- Path matchers (e.g. `@api`, `/static/*`) are indexed with their destination handlers.
- Security headers and TLS settings are preserved in the knowledge graph.
- Decommissioned site blocks or deleted routes are automatically purged from Cognee's memory via orphan cleanup.

## Installation

```bash
uv pip install cognee-community-connector-caddy
# or from this monorepo:
cd packages/connector/caddy && uv sync --all-extras
```

## Quick Start

```python
import cognee
from cognee_community_connector_caddy import caddy_source

# 1. Ingest Caddyfile snapshot into agent memory
await cognee.remember(
    caddy_source("Caddyfile"),
    dataset_name="web_routing",
    max_rows_per_table=0,  # ingest full routing snapshot
)

# 2. Build graph and semantic indices
await cognee.cognify()

# 3. Ask your routing questions!
answer = await cognee.search(
    query_text="What backend port does /api/* proxy to on app.company.com?",
    query_type=cognee.SearchType.GRAPH_COMPLETION,
    datasets=["web_routing"],
)
print(answer)
```

See `examples/example.py` for a runnable demo using sample fixtures in `examples/test_data/`.

## Architecture & Guarantees

* **Stable Primary Keys**: Site blocks are assigned deterministic IDs:
  `caddy:site:{primary_address_slug}`. Re-syncing unchanged configurations avoids duplicate processing.
* **Snapshot Sync (`replace`)**: Uses `write_disposition="replace"`, matching Cognee's dlt defaults. When a route or site block is removed from `Caddyfile`, cognee's orphan cleanup purges it from relational, graph, and vector stores.
* **Zero External Cloud Dependencies**: Operates locally on `Caddyfile` configurations with zero API tokens or rate limits.

## Testing

Run unit tests locally with `pytest`:

```bash
pytest packages/connector/caddy/tests
```
