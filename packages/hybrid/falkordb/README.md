# Cognee Community FalkorDB Hybrid Adapter

This is a community-maintained adapter that enables Cognee to work with FalkorDB as a hybrid graph database.

## Installation

```bash
pip install cognee-community-hybrid-adapter-falkor
```

## Usage

```python
import asyncio
import os
import pathlib
from os import path
from cognee import config, prune, add, cognify, search, SearchType

# Import the register module to enable FalkorDB support
from cognee_community_hybrid_adapter_falkor import register


async def main():
    # Set up local directories
    system_path = pathlib.Path(__file__).parent
    config.system_root_directory(path.join(system_path, ".cognee_system"))
    config.data_root_directory(path.join(system_path, ".cognee_data"))

    # Configure databases
    config.set_relational_db_config(
        {
            "db_provider": "sqlite",
        }
    )

    # Configure FalkorDB as both vector and graph database
    config.set_vector_db_config(
        {
            "vector_db_provider": "falkor",
            "vector_db_url": os.getenv("GRAPH_DB_URL", "localhost"),
            "vector_db_port": int(os.getenv("GRAPH_DB_PORT", "6379")),
        }
    )
    config.set_graph_db_config(
        {
            "graph_database_provider": "falkor",
            "graph_database_url": os.getenv("GRAPH_DB_URL", "localhost"),
            "graph_database_port": int(os.getenv("GRAPH_DB_PORT", "6379")),
        }
    )

    # Optional: Clean previous data
    await prune.prune_data()
    await prune.prune_system(metadata=True)

    # Add and process your content
    await add("""
    Natural language processing (NLP) is an interdisciplinary
    subfield of computer science and information retrieval.
    """)

    await add("""
    Sandwiches are best served toasted with cheese, ham, mayo,
    lettuce, mustard, and salt & pepper.          
    """)

    await cognify()

    # Search using graph completion
    query_text = "Tell me about NLP"
    search_results = await search(query_type=SearchType.GRAPH_COMPLETION, query_text=query_text)

    for result in search_results:
        print("\nSearch result: \n" + result)


if __name__ == "__main__":
    asyncio.run(main())
```

## Configuration

The FalkorDB adapter can be configured as both a vector database and graph database, providing true hybrid capabilities. The following configuration parameters are available:

**Graph Database Configuration:**
- `graph_database_provider`: Set to "falkordb"
- `graph_database_url`: Your FalkorDB server hostname (default: "localhost")
- `graph_database_port`: Your FalkorDB server port (default: 6379)

**Vector Database Configuration (optional - enables hybrid mode):**
- `vector_db_provider`: Set to "falkordb"
- `vector_db_url`: Your FalkorDB server hostname (default: "localhost")
- `vector_db_port`: Your FalkorDB server port (default: 6379)

### Environment Variables

Set the following environment variables or pass them directly in the config, or set the in the .env file:

```bash
export GRAPH_DB_URL="localhost"
export GRAPH_DB_PORT="6379"
export GRAPH_DATASET_DATABASE_HANDLER="falkor_graph_local"
export VECTOR_DATASET_DATABASE_HANDLER="falkor_vector_local"
```

**Alternative:** You can also use the [`.env.template`](https://github.com/topoteretes/cognee/blob/main/.env.template) file from the main cognee repository. Copy it to your project directory, rename it to `.env`, and fill in your FalkorDB configuration values.

## Requirements

- Python >= 3.11, <= 3.13
- falkordb >= 1.0.9, < 2.0.0
- cognee >= 0.2.0.dev0

## Features

- **True Hybrid Database**: Use FalkorDB as both vector and graph database in a single instance
- Full graph database capabilities with Cypher query support
- Vector similarity search and indexing
- Redis-based protocol support for high performance
- Hybrid storage and retrieval operations
- Async/await support
- Graph queries and traversals
- Knowledge graph construction and semantic analysis
- Unified data management across vector and graph domains

## About FalkorDB

FalkorDB is a graph database that combines the power of Redis with graph capabilities. It provides:
- High-performance graph operations
- Redis-compatible protocol
- Cypher query language support
- Real-time graph analytics
- Scalable graph storage

This adapter allows Cognee to leverage FalkorDB's hybrid capabilities for advanced knowledge graph operations and semantic search.
