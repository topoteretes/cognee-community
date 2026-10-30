# cognee-community-connector-docker-compose

A **Docker Compose (`docker-compose.yml`)** infrastructure data-source connector for [cognee](https://github.com/topoteretes/cognee): turn your multi-container services, port mappings, environment variables, dependencies, and networks into structured memory for AI DevOps agents.

It parses Docker Compose files and exposes a `dlt` resource you pass directly to `cognee.remember(...)`. It integrates cleanly with cognee's document ingestion pipeline, providing **snapshot sync** and **forget-on-delete** with zero core changes.

## Why This Matters for AI DevOps Agents

AI coding and DevOps assistants frequently hallucinate container port numbers, volume paths, internal network aliases, and service dependencies (`depends_on`).

By ingesting `docker-compose.yml` into Cognee:
- Agents know the **exact exposed and container port mappings** (e.g., `8080:8080`, `5432:5432`).
- Service dependency chains (`depends_on` conditions like `service_healthy`) are modeled into the knowledge graph.
- Environment variables and mounted volumes are indexed cleanly.
- Decommissioned services removed from `docker-compose.yml` are automatically purged from Cognee's memory via orphan cleanup.

## Installation

```bash
uv pip install cognee-community-connector-docker-compose
# or from this monorepo:
cd packages/connector/docker_compose && uv sync --all-extras
```

## Quick Start

```python
import cognee
from cognee_community_connector_docker_compose import docker_compose_source

# 1. Ingest compose snapshot into agent memory
await cognee.remember(
    docker_compose_source("docker-compose.yml"),
    dataset_name="infra_topology",
    max_rows_per_table=0,  # ingest the full topology
)

# 2. Build graph and semantic indices
await cognee.cognify()

# 3. Ask your infrastructure questions!
answer = await cognee.search(
    query_text="Which services depend on postgres, and what port is it mapped to?",
    query_type=cognee.SearchType.GRAPH_COMPLETION,
    datasets=["infra_topology"],
)
print(answer)
```

See `examples/example.py` for a runnable demo using sample fixtures in `examples/test_data/`.

## Architecture & Guarantees

* **Stable Primary Keys**: Services are assigned deterministic IDs:
  `docker_compose:service:{service_name}`. Re-syncing unchanged compose setups avoids duplicate processing.
* **Snapshot Sync (`replace`)**: Uses `write_disposition="replace"`, matching Cognee's dlt defaults. When a service is removed from `docker-compose.yml`, cognee's orphan cleanup purges it from relational, graph, and vector stores.
* **Zero External Cloud Dependencies**: Operates locally on `.yml` / `.yaml` compose files with zero API tokens or rate limits.

## Testing

Run unit tests locally with `pytest`:

```bash
pytest packages/connector/docker_compose/tests
```
