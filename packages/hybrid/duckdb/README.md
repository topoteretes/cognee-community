<div align="center" dir="auto">
    <img width="250" src="https://duckdb.org/images/logo-dl/DuckDB_Logo-stacked.svg" style="max-width: 100%" alt="DuckDB">
    <h1>🧠 Cognee DuckDB Vector Adapter</h1>
</div>

<div align="center" style="margin-top: 20px;">
    <span style="display: block; margin-bottom: 10px;">Lightning fast embedded vector search for Cognee using DuckDB with planned graph support</span>
    <br />

[![License: Apache 2.0](https://img.shields.io/badge/License-Apache%202.0-blue.svg)](https://opensource.org/licenses/Apache-2.0)
![Language](https://img.shields.io/badge/python-3.12+-blue.svg)

[![Powered by DuckDB](https://img.shields.io/badge/Powered%20by-DuckDB-yellow.svg)](https://duckdb.org)

</div>

<div align="center">
<div display="inline-block">
    <a href="https://github.com/topoteretes/cognee"><b>Cognee</b></a>&nbsp;&nbsp;&nbsp;
    <a href="https://duckdb.org/docs/"><b>DuckDB Docs</b></a>&nbsp;&nbsp;&nbsp;
    <a href="#examples"><b>Examples</b></a>&nbsp;&nbsp;&nbsp;
    <a href="#troubleshooting"><b>Support</b></a>
  </div>
    <br />
</div>


## Features

- **Zero-configuration** embedded vector database - no external server required
- Full support for vector embeddings storage and retrieval
- High-performance vector similarity search using DuckDB's native array operations
- Persistent or in-memory database options
- **Vector-first design** with planned graph support in future releases
- Comprehensive error handling and logging

## Installation

```bash
pip install cognee-community-hybrid-adapter-duckdb
```

## Prerequisites

**None!** DuckDB is an embedded database that requires no external dependencies or server setup. Just install and use.

## Examples
Checkout the `examples/` folder!

**Basic vector search example:**
```bash
uv run examples/example.py
```

**Document processing example with generated story:**
```bash
uv run examples/simple_document_example/cognee_simple_document_demo.py
```
This example demonstrates processing a generated story text file (`generated_story.txt`) along with other documents like Alice in Wonderland.

>You will need an OpenAI API key to run the example scripts.

## Usage

```python
import os
import asyncio
from cognee import config, prune, add, cognify, search, SearchType

# Import the register module to enable DuckDB support
from cognee_community_hybrid_adapter_duckdb import register


async def main():
    # Configure DuckDB as vector database
    config.set_vector_db_config(
        {
            "vector_db_provider": "duckdb",
            "vector_db_url": "my_database.db",  # File path or None for in-memory
        }
    )

    # Optional: Clean previous data
    await prune.prune_data()
    await prune.prune_system()

    # Add your content
    await add("""
    Natural language processing (NLP) is an interdisciplinary
    subfield of computer science and information retrieval.
    """)

    # Process with cognee
    await cognify()

    # Search (use vector-based search types)
    search_results = await search(query_type=SearchType.CHUNKS, query_text="Tell me about NLP")

    for result in search_results:
        print("Search result:", result)


if __name__ == "__main__":
    asyncio.run(main())
```

## Configuration

Configure DuckDB as your vector database in cognee:

- `vector_db_provider`: Set to "duckdb"
- `vector_db_url`: Database file path (e.g., "my_db.db"), `None` for in-memory, or MotherDuck URL for cloud

### Database Options

```python
# Persistent file-based database
config.set_vector_db_config({"vector_db_provider": "duckdb", "vector_db_url": "cognee_vectors.db"})

# In-memory database (fastest, but data is lost on restart)
config.set_vector_db_config(
    {
        "vector_db_provider": "duckdb",
        "vector_db_url": None,  # or ":memory:"
    }
)

# Absolute path to database file
config.set_vector_db_config(
    {"vector_db_provider": "duckdb", "vector_db_url": "/path/to/my/database.db"}
)

# MotherDuck cloud database
config.set_vector_db_config(
    {
        "vector_db_provider": "duckdb",
        "vector_db_url": "md:my_database",  # Replace with your MotherDuck database
    }
)
```

## Requirements

- Python >= 3.12, <= 3.13
- duckdb >= 1.3.2
- cognee >= 0.2.3

## Roadmap: Graph Support

This adapter is currently **vector-focused** with plans to add full graph database capabilities in future releases. The foundation is already in place with DuckDB's property graph extensions.

**Current Status:**
- ✅ Full vector similarity search
- ✅ Embedding storage and retrieval  
- ✅ Collection management
- 🚧 Graph operations (coming soon)

## Error Handling

The adapter includes comprehensive error handling:

- `CollectionNotFoundError`: Raised when attempting operations on non-existent collections
- `InvalidValueError`: Raised for invalid query parameters  
- `NotImplementedError`: Currently raised for graph operations (graph support coming soon)
- Graceful handling of database connection issues and embedding errors

## Performance

DuckDB provides excellent performance characteristics:

- **Embedded**: No network overhead - everything runs in-process
- **Columnar**: Optimized storage format for analytical workloads
- **Vectorized**: SIMD operations for fast vector similarity calculations
- **ACID**: Full transactional support with data consistency
- **Memory efficient**: Minimal memory footprint compared to traditional databases

## Troubleshooting

### Common Issues

1. **File Permission Errors**: Ensure write permissions to the directory containing your database file
2. **Embedding Dimension Mismatch**: Verify embedding dimensions match collection configuration
3. **Collection Not Found**: Always create collections before adding data points
4. **Graph Operations**: Graph support is planned for future releases - currently use vector search

### Debug Logging

The adapter uses Cognee's logging system. Enable debug logging to see detailed operation logs:

```python
import logging

logging.getLogger("DuckDBAdapter").setLevel(logging.DEBUG)
```

### Database Option Comparison

| Option | Pros | Cons |
|--------|------|------|
| File-based (`"my_db.db"`) | ✅ Persistent storage<br/>✅ Survives restarts<br/>✅ Can handle large datasets | ❌ Slower I/O<br/>❌ Disk space usage |
| In-memory (`None`) | ✅ Maximum performance<br/>✅ No disk usage<br/>✅ Perfect for testing | ❌ Data lost on restart<br/>❌ Limited by RAM |
| MotherDuck (`"md:database"`) | ✅ Cloud-hosted<br/>✅ Shared access<br/>✅ Managed service<br/>✅ Scalable | ❌ Requires internet<br/>❌ Potential latency<br/>❌ MotherDuck account needed |

## Development

To contribute or modify the adapter:

1. Clone the repository and `cd` into the `packages/hybrid/duckdb` folder
2. Install dependencies: `uv sync --all-extras`
3. Run tests: `uv run examples/example.py`
4. Make your changes, test, and submit a PR

## Extensions Used

This adapter automatically loads these DuckDB extensions:
- **duckpgq**: Property graph queries (foundation for upcoming graph support)
- **vss**: Vector similarity search with HNSW indexing support