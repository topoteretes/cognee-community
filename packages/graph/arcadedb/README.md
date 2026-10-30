# Cognee Community Graph Adapter - ArcadeDB

[ArcadeDB](https://arcadedb.com) graph database adapter for the [Cognee](https://github.com/topoteretes/cognee) framework.

ArcadeDB is an open-source (Apache 2.0) multi-model database supporting graph, document, key-value, full-text search, and vector embeddings. It implements the Neo4j Bolt wire protocol, so this adapter uses the standard `neo4j` Python async driver.

## Installation

```bash
pip install cognee-community-graph-adapter-arcadedb
```

## Prerequisites

ArcadeDB running with the Bolt protocol enabled (default port 7687):

```bash
docker run -d --name arcadedb -p 2480:2480 -p 7687:7687 \
  -e JAVA_OPTS="-Darcadedb.server.rootPassword=arcadedb -Darcadedb.server.defaultDatabases=cognee[root]{} -Darcadedb.server.plugins=Bolt:com.arcadedb.bolt.BoltProtocolPlugin" \
  arcadedata/arcadedb:latest
```

## Usage

```python
import cognee
from cognee_community_graph_adapter_arcadedb import register

# Register the adapter
cognee.config.set_graph_database_provider("arcadedb")
register()

# Configure connection
cognee.config.set_graph_db_config(
    {
        "graph_database_url": "bolt://localhost:7687",
        "graph_database_username": "root",
        "graph_database_password": "arcadedb",
    }
)

# Use cognee as usual
await cognee.add(["Your text data here"], "dataset_name")
await cognee.cognify(["dataset_name"])
results = await cognee.search(
    query_type=cognee.SearchType.GRAPH_COMPLETION,
    query_text="your query",
)
```

## Why ArcadeDB?

- **Multi-model**: Graph + Document + Key-Value + Full-text Search + Vector Embeddings in one engine
- **Bolt compatible**: Standard `neo4j` Python driver connects directly — no custom driver needed
- **OpenCypher**: 97.8% TCK compliance — standard Cypher queries work out of the box
- **Apache 2.0**: Truly open source, no BSL, no usage limits, self-hostable
- **Embeddable**: Can run embedded in Java applications for zero-latency access
