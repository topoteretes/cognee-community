<div align="center" dir="auto">
    <img width="250" src="https://raw.githubusercontent.com/redis/redis-vl-python/main/docs/_static/Redis_Logo_Red_RGB.svg" style="max-width: 100%" alt="Redis">
    <h1>🧠 Cognee Redis Vector Adapter</h1>
</div>

<div align="center" style="margin-top: 20px;">
    <span style="display: block; margin-bottom: 10px;">Blazing fast vector similarity search for Cognee using Redis</span>
    <br />

[![License: Apache 2.0](https://img.shields.io/badge/License-Apache%202.0-blue.svg)](https://opensource.org/licenses/Apache-2.0)
![Language](https://img.shields.io/badge/python-3.8+-blue.svg)

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

```bash
pip install cognee-community-vector-adapter-redis
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
from cognee.infrastructure.databases.vector.embeddings.EmbeddingEngine import EmbeddingEngine
from cognee_community_vector_adapter_redis import RedisAdapter

# Initialize your embedding engine
embedding_engine = EmbeddingEngine(
    model="your-model",
    # ... other config
)

# Create Redis adapter
redis_adapter = RedisAdapter(
    url="redis://localhost:6379",  # Redis connection URL
    embedding_engine=embedding_engine,
    api_key=None  # Optional, not used for Redis but part of interface
)

# Create a collection
await redis_adapter.create_collection("my_collection")

# Add data points
from cognee.infrastructure.engine import DataPoint

data_points = [
    DataPoint(id="1", text="Hello world", metadata={"index_fields": ["text"]}),
    DataPoint(id="2", text="Redis vector search", metadata={"index_fields": ["text"]})
]

await redis_adapter.create_data_points("my_collection", data_points)

# Search for similar vectors
results = await redis_adapter.search(
    collection_name="my_collection",
    query_text="Hello Redis",
    limit=10
)

# Search with pre-computed vector
query_vector = await redis_adapter.embed_data(["Hello Redis"])
results = await redis_adapter.search(
    collection_name="my_collection",
    query_vector=query_vector[0],
    limit=10,
    with_vector=True  # Include vectors in results
)

# Batch search
results = await redis_adapter.batch_search(
    collection_name="my_collection", 
    query_texts=["query1", "query2"],
    limit=5
)

# Retrieve specific data points
retrieved = await redis_adapter.retrieve(
    collection_name="my_collection",
    data_point_ids=["1", "2"]
)

# Delete data points
await redis_adapter.delete_data_points(
    collection_name="my_collection",
    data_point_ids=["1"]
)

# Check if collection exists
exists = await redis_adapter.has_collection("my_collection")
```

## Configuration

The Redis adapter supports the following configuration options:

- `url`: Redis connection URL (e.g., "redis://localhost:6379", "redis://user:pass@host:port")
- `embedding_engine`: The `EmbeddingEngine` to use for text vectorization (required)
- `api_key`: Optional API key parameter (not used for Redis but part of the interface)

### Connection URL Examples

```python
# Local Redis
redis_adapter = RedisAdapter(url="redis://localhost:6379", embedding_engine=engine)

# Redis with authentication
redis_adapter = RedisAdapter(url="redis://user:password@localhost:6379", embedding_engine=engine)

# Redis with SSL
redis_adapter = RedisAdapter(url="rediss://localhost:6380", embedding_engine=engine)
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
