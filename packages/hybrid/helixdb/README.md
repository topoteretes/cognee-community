# cognee-community-hybrid-adapter-helixdb

A community [cognee](https://github.com/topoteretes/cognee) adapter that uses
[HelixDB](https://www.helix-db.com/) as both the **graph** and the **vector**
store (hybrid adapter). Requires **cognee 1.4.1**.

HelixDB uses pre-compiled HQL queries; the adapter deploys a generic schema
(`CogneeNode` / `CogneeEdge` / `CogneeVector`) and a fixed set of query
endpoints at initialization time. Dynamic node types are stored as a
`node_type` property, arbitrary properties are JSON-serialized into
`properties_json`.

## Installation

```bash
pip install cognee-community-hybrid-adapter-helixdb
```

You need a running HelixDB instance (default `localhost:6969`). See the
[HelixDB docs](https://docs.helix-db.com/) for setup.

## Usage

```python
import asyncio

import cognee
from cognee_community_hybrid_adapter_helixdb import register  # noqa: F401


async def main():
    cognee.config.set_graph_db_config({"graph_database_provider": "helixdb"})
    cognee.config.set_vector_db_config({"vector_db_provider": "helixdb"})

    await cognee.add("HelixDB is a graph-vector database.")
    await cognee.cognify()

    results = await cognee.search("What is HelixDB?")
    print(results)


asyncio.run(main())
```

## Tests

```bash
pytest tests/unit      # offline contract tests, no server needed
```
