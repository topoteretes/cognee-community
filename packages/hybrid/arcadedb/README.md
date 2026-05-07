# Cognee Community ArcadeDB Hybrid Adapter

A community-maintained adapter that enables [Cognee](https://github.com/topoteretes/cognee) to work with [ArcadeDB](https://arcadedb.com/) as a unified hybrid database — both graph and vector operations in a single engine.

Based on [PR#94](https://github.com/topoteretes/cognee-community/pull/94) by [@lvca](https://github.com/lvca) (ArcadeDB team) with additional fixes for Cognee 1.0+ compatibility and production reliability.

## Installation

```bash
pip install cognee-community-hybrid-adapter-arcadedb

# Optional: Bolt protocol support (faster graph operations)
pip install "cognee-community-hybrid-adapter-arcadedb[bolt]"
```

## Usage

```python
import asyncio
import os
import pathlib
from os import path
from cognee import config, prune, add, cognify, search, SearchType

# Import the register module to enable ArcadeDB support
from cognee_community_hybrid_adapter_arcadedb import register

async def main():
    # Set up local directories
    system_path = pathlib.Path(__file__).parent
    config.system_root_directory(path.join(system_path, ".cognee_system"))
    config.data_root_directory(path.join(system_path, ".cognee_data"))

    # Configure databases
    config.set_relational_db_config({
        "db_provider": "sqlite",
    })

    # Configure ArcadeDB as both vector and graph database
    config.set_vector_db_config({
        "vector_db_provider": "arcadedb",
        "vector_db_url": os.getenv("ARCADEDB_URL", "localhost"),
        "vector_db_port": int(os.getenv("ARCADEDB_HTTP_PORT", "2480")),
        "vector_db_username": os.getenv("ARCADEDB_USERNAME", "root"),
        "vector_db_password": os.getenv("ARCADEDB_PASSWORD", ""),
    })
    config.set_graph_db_config({
        "graph_database_provider": "arcadedb",
        "graph_database_url": os.getenv("ARCADEDB_URL", "localhost"),
        "graph_database_port": int(os.getenv("ARCADEDB_HTTP_PORT", "2480")),
        "graph_database_username": os.getenv("ARCADEDB_USERNAME", "root"),
        "graph_database_password": os.getenv("ARCADEDB_PASSWORD", ""),
    })

    # Optional: Clean previous data
    await prune.prune_data()
    await prune.prune_system(metadata=True)

    # Add and process your content
    await add("""
    Natural language processing (NLP) is an interdisciplinary
    subfield of computer science and information retrieval.
    """)

    await cognify()

    # Search using graph completion
    search_results = await search(
        query_type=SearchType.GRAPH_COMPLETION,
        query_text="Tell me about NLP",
    )

    for result in search_results:
        print("\nSearch result:\n" + result)

if __name__ == "__main__":
    asyncio.run(main())
```

## Configuration

The ArcadeDB adapter can be configured as both a vector database and graph database, providing true hybrid capabilities.

### Environment Variables

```bash
export ARCADEDB_URL="localhost"
export ARCADEDB_HTTP_PORT="2480"
export ARCADEDB_USERNAME="root"
export ARCADEDB_PASSWORD="your-password"
export GRAPH_DATASET_DATABASE_HANDLER="arcadedb_graph_local"
export VECTOR_DATASET_DATABASE_HANDLER="arcadedb_vector_local"
```

### ArcadeDB Setup

ArcadeDB is a multi-model database that natively supports graph traversal, vector search, and document operations in a single engine.

**HTTP only (simplest):**

```bash
docker run -p 2480:2480 \
  -e JAVA_OPTS="-Darcadedb.server.rootPassword=your-password" \
  arcadedata/arcadedb:latest
```

**With Bolt protocol (recommended for better graph performance):**

```bash
docker run -p 2480:2480 -p 7687:7687 \
  -e JAVA_OPTS="-Darcadedb.server.rootPassword=your-password \
    -Darcadedb.server.plugins=Bolt:com.arcadedb.bolt.BoltProtocolPlugin" \
  arcadedata/arcadedb:latest
```

### Configuration Parameters

**Graph Database Configuration:**
- `graph_database_provider`: Set to `"arcadedb"`
- `graph_database_url`: ArcadeDB server hostname (default: `"localhost"`)
- `graph_database_port`: HTTP API port (default: `2480`)
- `graph_database_username`: Database username
- `graph_database_password`: Database password

**Vector Database Configuration:**
- `vector_db_provider`: Set to `"arcadedb"`
- `vector_db_url`: ArcadeDB server hostname (default: `"localhost"`)
- `vector_db_port`: HTTP API port (default: `2480`)
- `vector_db_username`: Database username
- `vector_db_password`: Database password

## How it works

### Graph operations

Graph operations use the **Neo4j Bolt protocol** when the `BoltProtocolPlugin` is enabled (port 7687), with automatic fallback to the **HTTP API** (OpenCypher). The adapter verifies Bolt connectivity on first use and transparently falls back if unavailable.

### Vector operations

Vector operations use ArcadeDB's HTTP API for SQL queries:

- **Index creation**: `CREATE INDEX ... LSM_VECTOR METADATA { dimensions: N, similarity: 'COSINE' }`
- **Vector storage**: Stored as `ARRAY_OF_FLOATS` properties on vertex records
- **Search**: `SELECT expand(vectorNeighbors('Type[prop]', [vec], k))` returns results sorted by distance

### Auto-detection

The adapter automatically:
- Detects the Cypher vertex type casing (`Vertex` vs `vertex`) based on ArcadeDB version
- Creates the target database on first use (if it doesn't exist)
- Retries on HTTP 503 `ConcurrentModificationException` (ArcadeDB uses optimistic concurrency)

## Key differences from PR#94

This adapter is based on [PR#94](https://github.com/topoteretes/cognee-community/pull/94) by [@lvca](https://github.com/lvca) with the following improvements:

| Area | Description |
|------|-------------|
| **Bug fix** | `index_data_points()` now delegates to `create_data_points()` for proper vector embedding and storage. Without this fix, vectors are never populated on TextSummary/DocumentChunk nodes, making vector search return empty results. |
| **Cognee 1.0+** | `get_neighborhood()` implementation for the new `GraphDBInterface` API |
| **Compatibility** | Auto-detection of Cypher type casing (ArcadeDB 26.3 vs 26.4+) |
| **Reliability** | Retry with backoff on HTTP 503 `ConcurrentModificationException` |
| **Database** | Lazy auto-creation with privilege-aware probing (from PR#94 commit `2b0b9f7`) |

## Requirements

- Python >= 3.11, <= 3.13
- ArcadeDB >= 26.3 (tested with 26.4.2)
- Cognee >= 1.0.3

## About ArcadeDB

ArcadeDB is a multi-model database engine that combines the power of graph databases, document stores, and vector search in a single, unified system:

- **Graph**: Full OpenCypher and Gremlin support, with Neo4j Bolt protocol compatibility
- **Vector**: Native HNSW/LSM_VECTOR indexes with cosine/euclidean similarity
- **Document**: JSON document storage with SQL queries
- **Multi-model**: Use all paradigms together in a single query

Key benefits:
- **No separate vector DB needed** — keep embeddings co-located with the knowledge graph
- **Distributed via Raft** — built-in high availability (no external coordination service)
- **Low latency** — optimized for fast traversals and real-time analytics
- **SQL + Cypher** — query in either language, or mix them

Learn more at [arcadedb.com](https://arcadedb.com/) and the [GitHub repository](https://github.com/ArcadeData/arcadedb).
