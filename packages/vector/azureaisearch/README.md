# Azure AI Search Adapter for Cognee

This adapter provides integration between Cognee and Azure AI Search (formerly Azure Cognitive Search) for vector storage and retrieval operations.

## Features

- Full vector search capabilities using Azure AI Search
- Hybrid search (combining text and vector search)
- HNSW algorithm for efficient similarity search
- Async/await support for all operations
- Batch operations for improved performance

## Installation

```bash
pip install -r requirements.txt
```

## Configuration

The adapter requires the following credentials:
- `endpoint`: Your Azure AI Search service endpoint (e.g., `https://your-service.search.windows.net`)
- `api_key`: Your Azure AI Search API key
- `embedding_engine`: An instance of EmbeddingEngine for text vectorization

## Usage

```python
from cognee.infrastructure.databases.vector.embeddings.EmbeddingEngine import EmbeddingEngine
from packages.vector.azureaisearch import AzureAISearchAdapter

# Initialize the adapter
embedding_engine = EmbeddingEngine(...)  # Your embedding engine
adapter = AzureAISearchAdapter(
    endpoint="https://your-service.search.windows.net",
    api_key="your-api-key",
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

## Key Differences from Other Vector Databases

1. **Collections as Indexes**: In Azure AI Search, what other vector databases call "collections" are called "indexes"
2. **Document Structure**: Documents in Azure AI Search have a specific schema with defined fields
3. **Batch Operations**: Azure AI Search doesn't have native batch search, so batch operations are parallelized
4. **Scoring**: Azure AI Search returns `@search.score` which is normalized differently than other vector databases

## Vector Search Configuration

The adapter uses HNSW (Hierarchical Navigable Small World) algorithm with the following default parameters:
- `m`: 4 (number of bi-directional links)
- `efConstruction`: 400 (size of the dynamic list)
- `efSearch`: 500 (size of the dynamic list for search)
- `metric`: cosine (similarity metric)

These parameters can be adjusted in the `create_collection` method if needed.
