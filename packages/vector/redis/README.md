# Redis Vector Store Adapter for Cognee

This package provides a Redis vector store adapter for Cognee, enabling vector similarity search using Redis' built-in vector search capabilities.

## Features

- Full support for vector embeddings storage and retrieval
- Cosine similarity search
- Batch operations for efficient processing
- Support for both exact (FLAT) and approximate (HNSW) vector search
- Async operations using aioredis

## Installation

```bash
pip install cognee-vector-redis
```

## Prerequisites

You need a Redis instance with the RediSearch module enabled. You can use:

1. **Redis Stack**:
   ```bash
   docker run -d --name redis-stack -p 6379:6379 -p 8001:8001 redis/redis-stack:latest
   ```

2. [**Redis Cloud**](https://redis.io/try-free) with RediSearch module enabled

3. **Redis Enterprise** with RediSearch module

## Usage

```python
from cognee.infrastructure.databases.vector.embeddings.EmbeddingEngine import EmbeddingEngine
from cognee_vector_redis import RedisAdapter

# Initialize your embedding engine
embedding_engine = EmbeddingEngine(
    model="your-model",
    # ... other config
)

# Create Redis adapter
redis_adapter = RedisAdapter(
    host="localhost",
    port=6379,
    password=None,  # Optional
    ssl=False,      # Set to True for SSL connections
    embedding_engine=embedding_engine
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

# Batch search
results = await redis_adapter.batch_search(
    collection_name="my_collection", 
    query_texts=["query1", "query2"],
    limit=5
)
```

## Configuration

The Redis adapter supports the following configuration options:

- `host`: Redis server hostname
- `port`: Redis server port (default: 6379)
- `password`: Redis password (optional)
- `ssl`: Enable SSL connection (default: False)
- `embedding_engine`: The embedding engine to use for text vectorization

## Index Configuration

The adapter creates indexes with the following default settings:

- Vector similarity metric: COSINE
- Vector type: FLOAT32
- Index type: FLAT (exact search)
- Storage format: JSON

You can modify the vector index algorithm by changing "FLAT" to "HNSW" in the `create_collection` method for approximate nearest neighbor search.

## Error Handling

The adapter includes proper error handling for common scenarios:

- Missing collections
- Connection failures  
- Invalid queries
- Embedding failures

## Performance Considerations

1. **Batch Operations**: Use `batch_search` for multiple queries to improve performance
2. **Index Algorithm**: Consider using HNSW for large datasets (>1M vectors)
3. **Connection Pooling**: The adapter creates a new connection for each operation. For high-throughput applications, consider implementing connection pooling
4. **Pipeline Operations**: The adapter uses Redis pipelines for batch inserts to improve performance

## Limitations

1. The adapter currently only supports COSINE distance metric
2. No support for metadata filtering in searches (can be added)
3. Batch search is implemented as parallel single searches

## Development

To contribute or modify the adapter:

1. Clone the repository
2. Install dependencies: `poetry install`
3. Run tests: `pytest`
4. Make your changes and submit a PR

## License

MIT License 