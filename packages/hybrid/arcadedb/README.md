# ArcadeDB Hybrid Adapter for Cognee

ArcadeDB is a multi-model database that natively supports **graph** and **vector** operations in a single engine. This adapter implements both `GraphDBInterface` and `VectorDBInterface`, allowing Cognee to use ArcadeDB as a unified graph+vector store.

## Architecture

- **Graph operations** use the Neo4j Bolt wire protocol (ArcadeDB supports Bolt natively), with standard OpenCypher queries via the `neo4j` async Python driver.
- **Vector operations** use ArcadeDB's HTTP API for SQL queries, leveraging native HNSW indexes and the `vectorNeighbors()` function for KNN search.

## Prerequisites

- ArcadeDB 24.x or later (with vector search support)
- Both Bolt (default port 7687) and HTTP (default port 2480) endpoints enabled

## Installation

```bash
pip install cognee-community-hybrid-adapter-arcadedb
```

## Configuration

Set these environment variables:

```bash
GRAPH_DATABASE_PROVIDER=arcadedb
GRAPH_DATABASE_URL=bolt://localhost:7687
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
    "graph_database_url": "bolt://localhost:7687",
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
Nodes and edges are stored using OpenCypher MERGE operations over Bolt. ArcadeDB's graph engine handles traversals, pattern matching, and relationship management.

### Vector Storage
Embeddings are stored as `ARRAY OF FLOAT` properties on vertices. HNSW indexes are created automatically via SQL:

```sql
CREATE INDEX ON `TypeName` (`property_vector`) NULL_STRATEGY SKIP HNSW 1536
```

Vector search uses ArcadeDB's built-in `vectorNeighbors()` function:

```sql
SELECT *, vectorNeighbors('TypeName', 'property_vector', [0.1, 0.2, ...], 10) AS score
FROM TypeName
WHERE vectorNeighbors('TypeName', 'property_vector', [0.1, 0.2, ...], 10) > 0
```

### Why Both Protocols?

ArcadeDB exposes multiple APIs. Graph operations naturally fit Cypher/Bolt (widely adopted, async driver available). Vector operations require ArcadeDB's SQL engine where `vectorNeighbors()` and HNSW indexes live. The adapter seamlessly bridges both.

## Features

- Single database for both knowledge graph and vector embeddings
- Native HNSW vector indexes with cosine similarity
- Full OpenCypher support for graph traversals
- Batch embedding and upsert support
- Multi-tenant dataset isolation
- No external vector database dependency needed
