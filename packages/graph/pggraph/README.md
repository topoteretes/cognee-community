# Cognee Community Graph Adapter - pgGraph (experimental)

Use [pgGraph](https://github.com/Evokoa/pgGraph) as a graph traversal layer on top of Cognee's
standard Postgres graph tables (`graph_node`, `graph_edge`).

**Status:** Experimental. Works without the pgGraph extension (SQL fallback). Install pgGraph on
Postgres for accelerated `graph.traverse()` queries.

## Installation

```bash
pip install cognee-community-graph-adapter-pggraph asyncpg
```

Or from this directory:

```bash
uv sync
```

## Quick demo (Docker + adapter test)

```bash
cd examples/docker
docker compose up -d
cd ..
export GRAPH_DATABASE_PROVIDER=pggraph
export GRAPH_DATABASE_HOST=localhost
export GRAPH_DATABASE_PORT=5433
export GRAPH_DATABASE_NAME=cognee
export GRAPH_DATABASE_USERNAME=cognee
export GRAPH_DATABASE_PASSWORD=cognee
export DB_PROVIDER=postgres
export DB_HOST=localhost
export DB_PORT=5433
export DB_NAME=cognee
export DB_USERNAME=cognee
export DB_PASSWORD=cognee
export ENABLE_BACKEND_ACCESS_CONTROL=false
uv run python examples/example.py
```

## Usage

```python
import asyncio
import os
import cognee
from cognee.infrastructure.databases.graph import get_graph_engine
from cognee_community_graph_adapter_pggraph import register


async def main():
    register()
    cognee.config.set_graph_database_provider("pggraph")
    cognee.config.set_graph_db_config({
        "graph_database_host": os.getenv("GRAPH_DATABASE_HOST", "localhost"),
        "graph_database_port": int(os.getenv("GRAPH_DATABASE_PORT", "5433")),
        "graph_database_name": os.getenv("GRAPH_DATABASE_NAME", "cognee"),
        "graph_database_username": os.getenv("GRAPH_DATABASE_USERNAME", "cognee"),
        "graph_database_password": os.getenv("GRAPH_DATABASE_PASSWORD", "cognee"),
    })

    graph = await get_graph_engine()
    await graph.initialize()
    # ... add_nodes, build_graph(), get_neighbors, etc.


asyncio.run(main())
```

## Environment variables

| Variable | Description |
|----------|-------------|
| `GRAPH_DATABASE_PROVIDER` | Set to `pggraph` after calling `register()` |
| `GRAPH_DATABASE_HOST` / `PORT` / `NAME` / `USERNAME` / `PASSWORD` | Postgres connection |
| `PGGRAPH_BUILD_MODE` | `manual` (default), `on_write`, or `scheduled` |
| `ENABLE_BACKEND_ACCESS_CONTROL` | Set `false` for single-database demos |

## pgGraph extension

The adapter checks `pg_available_extensions` before `CREATE EXTENSION graph`. To use the real
extension, run Postgres with pgGraph installed — see
[pgGraph quickstart](https://github.com/Evokoa/pgGraph).

## Core Cognee integration

This community package registers via `use_graph_adapter("pggraph", PgGraphAdapter)`. A future
upstream PR to [topoteretes/cognee](https://github.com/topoteretes/cognee) may add native
`GRAPH_DATABASE_PROVIDER=pggraph` support.

## Related

- [Evokoa/pgGraph](https://github.com/Evokoa/pgGraph)
- [pgGraph SQL API](https://docs.evokoa.com/pggraph/user_guide/api-reference)
