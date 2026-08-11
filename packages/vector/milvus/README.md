# Milvus Vector Database Adapter

This is a community-contributed adapter for integrating Milvus with Cognee.

## About Milvus

Milvus is an open-source vector database built to power AI applications. It provides high-performance similarity search and supports various index types, making it ideal for AI applications requiring fast and accurate vector searches.

## Installation

1. Install the required dependencies:
   ```bash
   # Option 1: Install dependencies directly
   pip install pymilvus>=2.5.0
   pip install milvus-lite>=2.4.0  # Linux/Mac only
   
   # Option 2: Install as a package (if published)
   pip install cognee-milvus-adapter
   
   # Option 3: Install from source
   cd community/adapters/vector/milvus
   pip install .
   ```

2. Import and register the adapter in your code:
   ```python
   from cognee_community_vector_adapter_milvus import register
   ```

## Configuration

Configure Cognee to use Milvus:

```python
# For local Milvus Lite
cognee.config.vector_db_provider("milvus")
cognee.config.vector_db_url("path/to/milvus.db")
cognee.config.vector_db_key("")  # No key needed for local

# For remote Milvus server
cognee.config.vector_db_provider("milvus")
cognee.config.vector_db_url("http://localhost:19530")  # Milvus server URL
cognee.config.vector_db_key("your_milvus_token")  # If authentication is enabled
```

## Usage Example

```python
import cognee

# Register the adapter (importing the module registers "milvus" with cognee)
from cognee_community_vector_adapter_milvus import register  # noqa: F401

# Configure Milvus
cognee.config.set_vector_db_config(
    {
        "vector_db_provider": "milvus",
        "vector_db_url": "./milvus.db",
        "vector_db_key": "",
    }
)

# Use Cognee normally
await cognee.add("Your data here")
await cognee.cognify()
results = await cognee.search("search query")
```

## Features

- **High-performance similarity search**: Optimized for large-scale vector operations
- **Multiple index types**: Supports various indexing algorithms (IVF_FLAT, IVF_SQ8, etc.)
- **Horizontal scaling**: Can handle billions of vectors
- **Hybrid search**: Combines vector similarity with scalar filtering
- **Enterprise-grade**: Production-ready with monitoring and management tools

## Testing

Run the tests to verify the adapter works correctly:

```bash
pytest tests/unit             # offline contract tests, no server needed
python tests/test_milvus.py   # integration test, needs Milvus + LLM keys
```

## Dependencies

- `pymilvus>=2.5.0,<3`: Official Milvus Python client
- `milvus-lite>=2.4.0`: Lightweight version of Milvus (Linux/Mac only)

## Deployment Options

### Local Development (Milvus Lite)
- Use `milvus-lite` for local development and testing
- No server setup required
- File-based storage

### Production (Milvus Server)
- Deploy Milvus server using Docker, Kubernetes, or cloud services
- Supports clustering and high availability
- Better performance for production workloads

## Support

For issues specific to this adapter:
1. Check the [Milvus documentation](https://milvus.io/docs)
2. Create an issue in the main Cognee repository with the "community-adapter" label
3. Refer to the example and test files for usage patterns

## License

This adapter is licensed under the Apache 2.0 license, same as the main Cognee project. 