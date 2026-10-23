# Portainer Connector for Cognee

A production-grade [Cognee](https://github.com/topoteretes/cognee) community data-source connector for **[Portainer](https://github.com/portainer/portainer)** (>33,000 GitHub stars), the universal container management platform for Docker, Docker Swarm, and Kubernetes.

This connector ingests cluster inventories, deployment stacks, Docker Compose definitions, and container runtime states into structured Markdown reports designed for Cognee's `cognify()` knowledge graph pipeline and semantic vector retrieval.

---

## Key Capabilities

- **Cluster & Environment Discovery**: Maps all registered Docker standalone engines, Swarm managers, Kubernetes clusters, and remote Edge agents.
- **Stack & Compose Ingestion**: Extracts stack names, deployment statuses, and raw docker-compose YAML definitions into memory.
- **Container Catalog**: Ingests container names, image digests, running states, health statuses, port forwardings, and volume mounts.
- **Document-Mode Routing**: Sets `DOCUMENT_SOURCE_ATTR = "portainer"` ensuring documents route through Cognee's knowledge graph extraction.
- **Orphan Cleanup (`write_disposition="replace"`)**: Automatically removes decommissioned containers and deleted stacks from graph memory.
- **Deterministic Document IDs**: Uses stable `uuid5` hashing so subsequent syncs idempotently update existing memory nodes without creating duplicates.
- **Security & Token Masking**: Automatically redacts API keys, JWTs, and passwords from logs, error messages, and tracebacks.

---

## Installation

```bash
pip install cognee cognee-community-connector-portainer
```

Or using `uv`:

```bash
uv add cognee cognee-community-connector-portainer
```

---

## Configuration Reference

The connector is configured using `PortainerConfig`:

| Parameter | Type | Default | Description |
|---|---|---|---|
| `api_url` | `str` | *required* | Base URL of Portainer instance (e.g. `https://portainer.local:9443`) |
| `api_key` | `str \| None` | `None` | Portainer API Key passed via `X-API-Key` |
| `jwt_token` | `str \| None` | `None` | Pre-existing JWT token passed via `Authorization: Bearer` |
| `username` | `str \| None` | `None` | Username for automatic token acquisition via `/api/auth` |
| `password` | `str \| None` | `None` | Password for automatic token acquisition via `/api/auth` |
| `endpoint_ids` | `list[int] \| None` | `None` | Specific environment IDs to ingest (or `None` for all) |
| `include_containers` | `bool` | `True` | Whether to extract container states and port mappings |
| `include_stacks` | `bool` | `True` | Whether to extract stack topologies and compose files |
| `request_timeout` | `float` | `30.0` | HTTP request timeout in seconds |

---

## Quickstart

```python
import asyncio
import os
import cognee
from cognee_community_connector_portainer import (
    PortainerConfig,
    portainer_connector,
)


async def main():
    config = PortainerConfig(
        api_url=os.getenv("PORTAINER_URL", "https://portainer.local:9443"),
        api_key=os.getenv("PORTAINER_API_KEY", "ptr_your_api_key"),
        include_containers=True,
        include_stacks=True,
    )

    # 1. Fetch telemetry documents
    documents = await portainer_connector(config)
    print(f"Retrieved {len(documents)} infrastructure documents.")

    # 2. Add to Cognee memory
    await cognee.add(documents, dataset_name="infrastructure_topology")

    # 3. Cognify into knowledge graph
    await cognee.cognify(dataset_name="infrastructure_topology")

    # 4. Query container and cluster topology
    query = "What containers are running on the primary cluster with port 6379 exposed?"
    results = await cognee.search(query, dataset_name="infrastructure_topology")
    for r in results:
        print(f"Memory match: {r}")


if __name__ == "__main__":
    asyncio.run(main())
```

---

## Running Offline Tests

The connector includes comprehensive offline unit tests that run with mocked HTTP transports (no live Portainer instance or network connection required):

```bash
uv run --project packages/connector/portainer --with pytest --with pytest-asyncio pytest packages/connector/portainer/tests -p no:typeguard -v
```

---

## License

Apache-2.0
