# cognee-community-connector-traefik

A Traefik reverse proxy and edge router connector for [cognee](https://github.com/topoteretes/cognee): sync your dynamic routers, backend services, security middlewares, and cluster topology into AI memory — *"Ask my reverse proxy infrastructure"*.

It exposes a `dlt` resource that feeds into `cognee.remember(...)`, integrating seamlessly with Cognee's DLT ingestion subsystem (`DOCUMENT_SOURCE_ATTR` routing → `cognify` entity/relationship extraction → `orphan_cleanup`).

## Features

- **Dynamic Topology Ingestion**: Discovers HTTP/TCP/UDP routers, backend services, and middlewares straight from Traefik's live API (`/api/http/routers`, `/api/http/services`, `/api/http/middlewares`, `/api/overview`, `/api/entrypoints`).
- **Security & Middleware Visibility**: Maps RateLimit parameters, Basic/Digest Auth, ForwardAuth endpoints, IP Allow Lists, and security headers directly to the routes they protect.
- **Incremental Synchronization**: Calculates deterministic SHA-256 fingerprints for each entity. Re-running `remember()` processes only routers or services whose definitions or health targets changed.
- **Forget-on-Delete**: When dynamic containers stop or routes are removed from Traefik, tombstone records (`_deleted=True`) are emitted on `merge`, triggering Cognee's `orphan_cleanup` to purge obsolete nodes and edges from the knowledge graph.
- **Fail-Closed Reliability**: Network timeouts or API 5xx errors halt execution safely without corrupting or wiping existing graph memory.

## Requirements

- Python 3.10 to 3.14.
- `cognee >= 1.6.2, < 2`
- `dlt[sqlalchemy] >= 1.9.0, < 2`
- `requests >= 2.31.0, < 3`
- A reachable Traefik v2 or v3 API endpoint (e.g. `http://localhost:8080`).

## Installation

```bash
uv pip install cognee-community-connector-traefik
# or in development:
cd packages/connector/traefik && uv sync
```

## Quick Start

```python
import asyncio
import cognee
from cognee_community_connector_traefik import traefik_source


async def main():
    # 1. Ingest Traefik topology into memory
    source = traefik_source(
        api_url="http://localhost:8080",
        resource_name="traefik_production",
    )

    await cognee.remember(
        source,
        dataset_name="traefik_infra",
        primary_key="id",
        write_disposition="merge",
        max_rows_per_table=0,
    )

    # 2. Ask natural language infrastructure questions
    answer = await cognee.search(
        query_text="Which public HTTP routers do not have rate-limiting or authentication middlewares attached?",
        dataset_name="traefik_infra",
    )
    print(answer)


if __name__ == "__main__":
    asyncio.run(main())
```

## Configuration & Authentication

The connector supports all standard Traefik authentication mechanisms:

```python
# Unauthenticated (default local dev / internal cluster)
source = traefik_source(api_url="http://traefik.internal:8080")

# Bearer Token
source = traefik_source(
    api_url="https://traefik.example.com",
    token="your-api-bearer-token",
)

# HTTP Basic Authentication
source = traefik_source(
    api_url="https://traefik.example.com",
    username="admin",
    password="supersecretpassword",
)
```

Environment variables can also be used:
- `TRAEFIK_API_URL`
- `TRAEFIK_API_TOKEN`
- `TRAEFIK_USERNAME`
- `TRAEFIK_PASSWORD`

## Ingested Objects & Markdown Documents

Each Traefik entity is converted into a structured Markdown document optimized for LLM reasoning and Cognee graph extraction:

| Entity | ID Format | Ingested Details |
|---|---|---|
| **HTTP Router** | `traefik:http:router:{name}` | Rule matching syntax (`Host(...) && PathPrefix(...)`), entrypoints, attached service, middlewares, TLS status & cert resolver, priority, status |
| **TCP Router** | `traefik:tcp:router:{name}` | SNI routing rules, entrypoints, backend services |
| **HTTP Service** | `traefik:http:service:{name}` | Load-balancer servers, weights, health check path/interval/timeout, status |
| **Middleware** | `traefik:http:middleware:{name}` | Middleware type (RateLimit, ForwardAuth, BasicAuth, IPAllowList, Headers), rate limits, auth endpoints, allowed CIDRs |
| **Topology Overview** | `traefik:cluster:overview` | Cluster entrypoints and port bindings, aggregate router/service/middleware health counts |

## Testing

The package includes 100% offline, hermetic unit tests with zero live Traefik dependencies:

```bash
pytest
```
