# Cognee Community HelixDB Hybrid Adapter

This is a community-maintained adapter that enables Cognee to work with [HelixDB](https://www.helix-db.com/) as a hybrid graph-vector database.

HelixDB is an open-source graph-vector database built in Rust that natively supports both graph and vector operations, making it a true hybrid backend for cognee.

## Prerequisites

- Python >= 3.11, <= 3.13
- HelixDB running locally or remotely
- cognee == 0.5.3

### Installing HelixDB

Follow the [HelixDB installation guide](https://docs.helix-db.com/) to install and start a HelixDB instance:

```bash
# Start HelixDB with the dashboard
helix dashboard start --host localhost --helix-port 6969
```

## Installation

```bash
pip install cognee-community-hybrid-adapter-helixdb
```

Or for development:

```bash
cd packages/hybrid/helixdb
pip install -e .
```

## Usage

```python
import asyncio
import os
import pathlib
from os import path
from cognee import config, prune, add, cognify, search, SearchType

# Import the register module to enable HelixDB support
from cognee_community_hybrid_adapter_helixdb import register

async def main():
    # Set up local directories
    system_path = pathlib.Path(__file__).parent
    config.system_root_directory(path.join(system_path, ".cognee_system"))
    config.data_root_directory(path.join(system_path, ".cognee_data"))

    # Configure HelixDB as both vector and graph database
    config.set_vector_db_config({
        "vector_db_provider": "helixdb",
        "vector_db_url": os.getenv("GRAPH_DB_URL", "localhost"),
        "vector_db_port": int(os.getenv("GRAPH_DB_PORT", "6969")),
    })
    config.set_graph_db_config({
        "graph_database_provider": "helixdb",
        "graph_database_url": os.getenv("GRAPH_DB_URL", "localhost"),
        "graph_database_port": int(os.getenv("GRAPH_DB_PORT", "6969")),
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
    query_text = "Tell me about NLP"
    search_results = await search(
        query_type=SearchType.GRAPH_COMPLETION,
        query_text=query_text
    )

    for result in search_results:
        print("\nSearch result: \n" + result)

if __name__ == "__main__":
    asyncio.run(main())
```

## Configuration

The HelixDB adapter serves as both a vector database and graph database, providing true hybrid capabilities. The following configuration parameters are available:

**Graph Database Configuration:**
- `graph_database_provider`: Set to `"helixdb"`
- `graph_database_url`: Your HelixDB server hostname (default: `"localhost"`)
- `graph_database_port`: Your HelixDB server port (default: `6969`)

**Vector Database Configuration (enables hybrid mode):**
- `vector_db_provider`: Set to `"helixdb"`
- `vector_db_url`: Your HelixDB server hostname (default: `"localhost"`)
- `vector_db_port`: Your HelixDB server port (default: `6969`)

### Environment Variables

Set the following environment variables or pass them directly in the config, or set them in the `.env` file:

```bash
export GRAPH_DATABASE_PROVIDER=helixdb
export GRAPH_DATABASE_URL=localhost
export GRAPH_DATABASE_PORT=6969
export VECTOR_DB_PROVIDER=helixdb
export VECTOR_DB_URL=localhost
export VECTOR_DB_PORT=6969
export GRAPH_DATASET_DATABASE_HANDLER=helixdb_graph_local
export VECTOR_DATASET_DATABASE_HANDLER=helixdb_vector_local
```

**Alternative:** You can also use the [`.env.template`](https://github.com/topoteretes/cognee/blob/main/.env.template) file from the main cognee repository. Copy it to your project directory, rename it to `.env`, and fill in your HelixDB configuration values.

## How It Works

### Generic Schema

Since cognee creates node types dynamically, the adapter uses a generic schema with three types:

- **CogneeNode**: Stores all graph nodes with `node_id`, `node_type`, `name`, and a `properties_json` field for arbitrary properties
- **CogneeEdge**: Stores relationships between nodes with `relationship_name` and serialised edge properties
- **CogneeVector**: Stores vector embeddings alongside metadata (`collection_name`, `text`, `properties_json`)

### Pre-compiled Queries

HelixDB requires queries to be pre-compiled in HQL (Helix Query Language). The adapter deploys a fixed set of query endpoints at initialisation:

- **Node CRUD**: `upsert_node`, `get_node`, `get_all_nodes`, `delete_node`
- **Edge CRUD**: `add_edge`, `get_edges_out`, `get_edges_in`, `get_all_edges`
- **Traversal**: `get_outgoing_neighbors`, `get_incoming_neighbors`
- **Vector**: `add_vector`, `search_vectors`

### Schema Deployment

On first connection the adapter writes `schema.hx` and `queries.hx` to a temporary directory and deploys them via `helix.Instance`. If the schema is already deployed (or the instance is remote), this step is silently skipped.

## Features

- **True Hybrid Database**: Use HelixDB as both vector and graph database in a single instance
- Graph database capabilities with HelixQL query support
- Vector similarity search (cosine similarity, F64 vectors)
- Async/await support (synchronous SDK calls wrapped in `run_in_executor`)
- Knowledge graph construction and semantic analysis
- Unified data management across vector and graph domains
- UnifiedStoreEngine integration on cognee's `dev` branch

## About HelixDB

[HelixDB](https://www.helix-db.com/) is an open-source graph-vector database built from scratch in Rust. It provides:

- High-performance graph and vector operations
- HelixQL: a compiled, type-safe query language
- Native vector search with cosine similarity
- Python SDK (`helix-py`) for easy integration

## Requirements

- Python >= 3.11, <= 3.13
- helix-py >= 0.2.30
- cognee == 0.5.3

## Limitations

- **Vector deletion**: HelixDB does not support deleting vectors independently. The adapter deletes the containing node instead.
- **Vectors use F64 only**: All vector dimensions must use 64-bit floats.
- **Pre-compiled queries**: All query endpoints must be defined at deployment time. The adapter ships a fixed set of generic queries.
