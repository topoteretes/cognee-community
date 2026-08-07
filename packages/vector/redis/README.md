<div align="center" dir="auto">
    <img width="250" src="https://raw.githubusercontent.com/redis/redis-vl-python/main/docs/_static/Redis_Logo_Red_RGB.svg" style="max-width: 100%" alt="Redis">
    <h1>🧠 Cognee Redis Vector Adapter</h1>
</div>

<div align="center" style="margin-top: 20px;">
    <span style="display: block; margin-bottom: 10px;">Blazing fast vector similarity search for Cognee using Redis</span>
    <br />

[![License: Apache 2.0](https://img.shields.io/badge/License-Apache%202.0-blue.svg)](https://opensource.org/licenses/Apache-2.0)
![Language](https://img.shields.io/badge/python-3.11+-blue.svg)

[![Powered by RedisVL](https://img.shields.io/badge/Powered%20by-RedisVL-red.svg)](https://github.com/redis/redis-vl-python)

</div>

<div align="center">
<div display="inline-block">
    <a href="https://github.com/topoteretes/cognee"><b>Cognee</b></a>&nbsp;&nbsp;&nbsp;
    <a href="https://docs.redisvl.com"><b>RedisVL Docs</b></a>&nbsp;&nbsp;&nbsp;
    <a href="#examples"><b>Examples</b></a>&nbsp;&nbsp;&nbsp;
    <a href="#troubleshooting"><b>Support</b></a>
  </div>
    <br />
</div>


## Features

- Full support for vector embeddings storage and retrieval
- Batch / pipeline operations for efficient processing
- Automatic embedding generation via configurable embedding engines
- JSON payload serialization with UUID support
- Comprehensive error handling

## Installation

If published, the package can be simply installed via pip:

```bash
pip install cognee-community-vector-adapter-redis
```

In case it is not published yet, you can use poetry to locally build the adapter package:

```bash
pip install poetry
poetry install # run this command in the directory containing the pyproject.toml file
```

## Prerequisites

You need a Redis instance with the Redis Search module enabled. You can use:

1. **Redis**:
   ```bash
   docker run -d --name redis -p 6379:6379 redis:8.0.2
   ```

2. **Redis Cloud** with the search module enabled: [Redis Cloud](https://redis.io/try-free)

## Examples
Checkout the `examples/` folder!

```bash
uv run examples/example.py
```

>You will need an OpenAI API key to run the example script.

## Usage

```python
import os
import asyncio
from cognee import config, prune, add, cognify, search, SearchType

# Import the register module to enable Redis support
from cognee_community_vector_adapter_redis import register


async def main():
    # Configure Redis as vector database
    config.set_vector_db_config(
        {
            "vector_db_provider": "redis",
            "vector_db_url": os.getenv("VECTOR_DB_URL", "redis://localhost:6379"),
            "vector_db_key": os.getenv("VECTOR_DB_KEY", "your-api-key"),  # Optional
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

    # Search
    search_results = await search(
        query_type=SearchType.GRAPH_COMPLETION, query_text="Tell me about NLP"
    )

    for result in search_results:
        print("Search result:", result)


if __name__ == "__main__":
    asyncio.run(main())
```

## Configuration

Configure Redis as your vector database in cognee:

- `vector_db_provider`: Set to "redis"
- `vector_db_url`: Redis connection URL (e.g., "redis://localhost:6379")
- `vector_db_key`: Optional API key parameter (for compatibility, not used by Redis)

### Environment Variables

Set the following environment variables or pass them directly in the config:

```bash
export VECTOR_DB_URL="redis://localhost:6379"
export VECTOR_DB_KEY="optional-key"  # Not used by Redis
```

### Connection URL Examples

```python
# Local Redis
config.set_vector_db_config(
    {"vector_db_provider": "redis", "vector_db_url": "redis://localhost:6379"}
)

# Redis with authentication
config.set_vector_db_config(
    {"vector_db_provider": "redis", "vector_db_url": "redis://user:password@localhost:6379"}
)

# Redis with SSL
config.set_vector_db_config(
    {"vector_db_provider": "redis", "vector_db_url": "rediss://localhost:6380"}
)
```

## Requirements

- Python >= 3.11, <= 3.13
- redisvl >= 0.6.0, <= 1.0.0
- cognee == 1.4.1

## Advanced Usage

For direct adapter usage (advanced users only):

```python
from cognee.infrastructure.databases.vector.embeddings.EmbeddingEngine import EmbeddingEngine
from cognee_community_vector_adapter_redis import RedisAdapter
from cognee.infrastructure.engine import DataPoint

# Initialize embedding engine and adapter
embedding_engine = EmbeddingEngine(model="your-model")
redis_adapter = RedisAdapter(url="redis://localhost:6379", embedding_engine=embedding_engine)

# Direct adapter operations
await redis_adapter.create_collection("my_collection")
data_points = [DataPoint(id="1", text="Hello", metadata={"index_fields": ["text"]})]
await redis_adapter.create_data_points("my_collection", data_points)
results = await redis_adapter.search("my_collection", query_text="Hello", limit=10)
```

## Error Handling

The adapter includes comprehensive error handling:

- `VectorEngineInitializationError`: Raised when required parameters are missing
- `CollectionNotFoundError`: Raised when attempting operations on non-existent collections
- `InvalidValueError`: Raised for invalid query parameters
- Graceful handling of connection failures and embedding errors


## Troubleshooting

### Common Issues

1. **Connection Errors**: Ensure Redis is running and accessible at the specified URL
2. **Search Module Missing**: Make sure Redis has the Search module enabled
3. **Embedding Dimension Mismatch**: Verify embedding engine dimensions match index configuration
4. **Collection Not Found**: Always create collections before adding data points

### Debug Logging

The adapter uses Cognee's logging system. Enable debug logging to see detailed operation logs:

```python
import logging

logging.getLogger("RedisAdapter").setLevel(logging.DEBUG)
```

## Development

To contribute or modify the adapter:

1. Clone the repository and `cd` into the `redis` folder
2. Install dependencies: `uv sync --all-extras`
3. Make sure a Redis instance is running (see above)
5. Make your changes, test, and submit a PR
