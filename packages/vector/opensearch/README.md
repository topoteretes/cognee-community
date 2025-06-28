# OpenSearch Adapter for Cognee

This adapter provides integration between Cognee and [OpenSearch](https://opensearch.org/) for vector storage and retrieval operations.

## Features

- Full vector search capabilities using OpenSearch;
- Hybrid search (combining text and vector search);
- HNSW algorithm for efficient similarity search (NOTE: For now, the algorithm is not configurable in the adapter. New versions may allow for more flexibility in the near future.);
- Async/await support for all operations;
- Batch operations for improved performance

## Installation

```bash
pip install .
```

## Configuration

The adapter requires the following credentials:
- `url`: The URL of your OpenSearch instance, including the port if necessary (e.g., `https://your-open-search-url:9200`);
- `api_key`: A base64 encoded string of a JSON object containing connection parameters:
  - `username`: Your OpenSearch username;
  - `password`: Your OpenSearch password;
  - `use_ssl`: Whether to use SSL (True/False);
  - `verify_certs`: Whether to verify SSL certificates (True/False);
  - `ssl_assert_hostname`: Whether to assert the hostname in SSL (True/False);
  - `ssl_show_warn`: Whether to show SSL warnings (True/False);
  - `index_prefix`: A prefix for the index names used by the adapter.
- `embedding_engine`: An instance of EmbeddingEngine for text vectorization

## Usage

```python
from cognee.infrastructure.databases.vector.embeddings.EmbeddingEngine import EmbeddingEngine
from packages.vector.cognee_community_vector_adapter_opensearch.cognee_community_vector_adapter_opensearch import OpenSearchAdapter
import json
import base64

# Creating the api_key as a base64 encoded string from the json string of the parameters
connection_parameters = {
    "username": "my-username",
    "password": "my-password",
    "use_ssl": "False",
    "verify_certs": "False",
    "ssl_assert_hostname": "False",
    "ssl_show_warn": "False",
    "index_prefix": "my-special-app-prefix-",
}

api_key = base64.b64encode(json.dumps(connection_parameters).encode()).decode()

# Initialize the adapter
embedding_engine = EmbeddingEngine(...)  # Your embedding engine
adapter = OpenSearchAdapter(
    url="https://your-open-search-url-including-port-if-any",
    api_key=api_key,
    embedding_engine=embedding_engine
)

# Create a collection (index)
await adapter.create_collection("my_collection")

# Add data points
await adapter.create_data_points("my_collection", data_points)

# Search
results = await adapter.search(
    collection_name="my_collection",
    query_text="search query",
    limit=10
)

# Batch search
results = await adapter.batch_search(
    collection_name="my_collection",
    query_texts=["query1", "query2"],
    limit=10
)
```
