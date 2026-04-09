# ArcadeDB Hybrid Adapter for Cognee

ArcadeDB is a multi-model database that natively supports **graph** and **vector** operations in a single engine. This adapter implements both `GraphDBInterface` and `VectorDBInterface`, allowing Cognee to use ArcadeDB as a unified graph+vector store.

## Architecture

All communication uses ArcadeDB's HTTP API:

- **Graph operations** use OpenCypher queries (language="cypher") with parameterized queries
- **Vector operations** use SQL (language="sql") for LSM_VECTOR index creation and `vectorNeighbors()` KNN search

## Prerequisites

- ArcadeDB 24.x or later (with vector search support)
- HTTP endpoint enabled (default port 2480)

## Installation

```bash
pip install cognee-community-hybrid-adapter-arcadedb
```

## Configuration

Set these environment variables:

```bash
GRAPH_DATABASE_PROVIDER=arcadedb
GRAPH_DATABASE_URL=localhost
GRAPH_DATABASE_USERNAME=root
GRAPH_DATABASE_PASSWORD=your_password

VECTOR_DB_PROVIDER=arcadedb
VECTOR_DB_URL=localhost
```

Or configure in Python:

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
Nodes and edges are stored using OpenCypher MERGE operations via HTTP. ArcadeDB's graph engine handles traversals, pattern matching, and relationship management.

### Vector Storage
Embeddings are stored as `ARRAY_OF_FLOATS` properties on vertices. LSM_VECTOR (HNSW) indexes are created automatically:

```sql
CREATE PROPERTY `TypeName`.`text_vector` IF NOT EXISTS ARRAY_OF_FLOATS

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
- Native HNSW (LSM_VECTOR) indexes with cosine similarity
- Full OpenCypher support for graph traversals
- Batch embedding and upsert support
- Multi-tenant dataset isolation
- No external vector database dependency needed
- Only requires a single HTTP endpoint (port 2480)
