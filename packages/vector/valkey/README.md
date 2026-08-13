# Cognee Valkey Vector Adapter

A Valkey vector database adapter for Cognee using Valkey Glide, providing high-performance vector storage and retrieval for AI memory applications. Compared to the Redis adapter, Valkey offers a fully open-source, community-driven architecture without the licensing restrictions of Redis. Using Valkey Glide ensures efficient async operations and native support for Valkey’s enhancements, providing optimal compatibility and performance when running on Valkey, making it the best choice for teams adopting Valkey as their primary in-memory vector solution.

## Features

- Full support for vector embeddings storage and retrieval
- Batch / pipeline operations for efficient processing
- Automatic embedding generation via configurable embedding engines
- Comprehensive error handling

## Installation

If published, the package can be simply installed via pip:

```bash
pip install cognee-community-vector-adapter-valkey
```

In case it is not published yet, you can use poetry to locally build the adapter package:

```bash
pip install uv
uv sync --all-extras
```

## Prerequisites

You need a Valkey instance with the Valkey Search module enabled. You can use:

1. **Valkey**:
   ```bash
   docker run -d --name valkey -p 6379:6379 valkey/valkey-bundle
   ```
   
## Examples
Checkout the `examples/` folder!

```bash
uv run examples/example.py
```

>You will need an OpenAI API key to run the example script.

## Configuration

Configure Valkey as your vector database in cognee:

- `vector_db_provider`: Set to "valkey"
- `vector_db_url`: Valkey connection URL (e.g., "valkey://localhost:6379")

### Environment Variables

Set the following environment variables or pass them directly in the config:

```bash
export VECTOR_DB_URL="valkey://localhost:6379"
```

### Connection URL Examples

```python
# Local Valkey
config.set_vector_db_config(
    {"vector_db_provider": "valkey", "vector_db_url": "valkey://localhost:6379"}
)

# Valkey with authentication
config.set_vector_db_config(
    {"vector_db_provider": "valkey", "vector_db_url": "valkey://user:password@localhost:6379"}
)
```

## Requirements

- Python >= 3.11, <= 3.13
- valkey-glide >= 2.1.0
- cognee == 1.4.2

## Advanced Usage

For direct adapter usage (advanced users only):

```python
from cognee.infrastructure.databases.vector.embeddings.EmbeddingEngine import EmbeddingEngine
from cognee_community_vector_adapter_valkey import ValkeyAdapter
from cognee.infrastructure.engine import DataPoint

# Initialize embedding engine and adapter
embedding_engine = EmbeddingEngine(model="your-model")
valkey_adapter = ValkeyAdapter(url="valkey://localhost:6379", embedding_engine=embedding_engine)

# Direct adapter operations
await valkey_adapter.create_collection("my_collection")
data_points = [DataPoint(id="1", text="Hello", metadata={"index_fields": ["text"]})]
await valkey_adapter.create_data_points("my_collection", data_points)
results = await valkey_adapter.search("my_collection", query_text="Hello", limit=10)
```

## Error Handling

The adapter includes comprehensive error handling:

- `VectorEngineInitializationError`: Raised when required parameters are missing
- `CollectionNotFoundError`: Raised when attempting operations on non-existent collections
- `InvalidValueError`: Raised for invalid query parameters
- Graceful handling of connection failures and embedding errors


## Troubleshooting

### Common Issues

1. **Connection Errors**: Ensure Valkey is running and accessible at the specified URL
2. **Search Module Missing**: Make sure Valkey has the Search module enabled
3. **Embedding Dimension Mismatch**: Verify embedding engine dimensions match index configuration
4. **Collection Not Found**: Always create collections before adding data points

### Debug Logging

The adapter uses Cognee's logging system. Enable debug logging to see detailed operation logs:

```python
import logging

logging.getLogger("ValkeyAdapter").setLevel(logging.DEBUG)
```

## Development

To contribute or modify the adapter:

1. Clone the repository and `cd` into the `valkey` folder
2. Install dependencies: `uv sync --all-extras`
3. Make sure a Valkey instance is running (see above)
5. Make your changes, test, and submit a PR
