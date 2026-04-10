# ArcadeDB Hybrid Adapter for Cognee

ArcadeDB is a multi-model database that natively supports **graph** and **vector** operations in a single engine. This adapter implements both `GraphDBInterface` and `VectorDBInterface`, allowing Cognee to use ArcadeDB as a unified graph+vector store.

## Architecture

- **Graph operations** use OpenCypher queries via either:
  - **Bolt protocol** (binary, faster) if the `BoltProtocolPlugin` is enabled and `neo4j` is installed
  - **HTTP API** (fallback) if Bolt is not available
- **Vector operations** always use the HTTP API with SQL for `LSM_VECTOR` index creation and `vectorNeighbors()` KNN search

The adapter automatically detects Bolt availability on first use and falls back to HTTP transparently.

## Prerequisites

- ArcadeDB 26.2.1 or later
- HTTP endpoint enabled (default port 2480)
- (Optional) Bolt plugin enabled for better graph performance (port 7687)

## Installation

```bash
# HTTP only (works out of the box)
pip install cognee-community-hybrid-adapter-arcadedb

# With Bolt protocol support (recommended for production)
pip install cognee-community-hybrid-adapter-arcadedb[bolt]
```

## ArcadeDB Setup

### HTTP only (simplest)

```bash
docker run -p 2480:2480 \
  -e JAVA_OPTS="-Darcadedb.server.rootPassword=your_password" \
  arcadedata/arcadedb:latest
```

### With Bolt protocol (recommended)

```bash
docker run -p 2480:2480 -p 7687:7687 \
  -e JAVA_OPTS="-Darcadedb.server.rootPassword=your_password \
     -Darcadedb.server.plugins=Bolt:com.arcadedb.bolt.BoltProtocolPlugin" \
  arcadedata/arcadedb:latest
```

## Configuration

```python
import cognee

cognee.config.set_graph_db_config({
    "graph_database_provider": "arcadedb",
    "graph_database_url": "localhost",
    "graph_database_username": "root",
    "graph_database_password": "your_password",
})

cognee.config.set_vector_db_config({
    "vector_db_provider": "arcadedb",
    "vector_db_url": "localhost",
})
```

## How It Works

### Graph Storage
Nodes and edges are stored using OpenCypher MERGE operations. When the Bolt plugin is enabled, the adapter uses the `neo4j` Python driver for binary protocol communication. Otherwise, it sends Cypher queries over the HTTP API.

### Vector Storage
Embeddings are stored as `ARRAY_OF_FLOATS` properties on vertices. LSM_VECTOR (HNSW) indexes are created automatically:

```sql
CREATE INDEX ON `TypeName` (`text_vector`) LSM_VECTOR METADATA {
  dimensions: 1536,
  similarity: 'COSINE'
}
```

Vector search uses ArcadeDB's built-in `vectorNeighbors()` function:

```sql
SELECT *, distance FROM (
  SELECT expand(vectorNeighbors('TypeName[text_vector]', [0.1, 0.2, ...], 10))
)
```

## Features

- Single database for both knowledge graph and vector embeddings
- Automatic Bolt/HTTP protocol selection (Bolt preferred, HTTP fallback)
- Native HNSW (LSM_VECTOR) indexes with cosine similarity
- Full OpenCypher support for graph traversals
- Batch embedding and upsert support
- Multi-tenant dataset isolation
- No external vector database dependency needed
