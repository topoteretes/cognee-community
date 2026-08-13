# Cognee Community Weaviate Vector Adapter

This is a community-maintained adapter that enables Cognee to work with Weaviate as a vector database.

## Installation

If published, the package can be simply installed via pip:

```bash
pip install cognee-community-vector-adapter-weaviate
```

In case it is not published yet, you can use poetry to locally build the adapter package:

```bash
pip install poetry
poetry install # run this command in the directory containing the pyproject.toml file
```

## Connection Setup
The provided code creates an async client connected to a remote instance of Weaviate. If you want to connect to a local 
instance, like running a docker container locally and connecting to it, you need to change a few lines of code. 
In the `weaviate_adapter.py` file inside the `.../weaviate/cognee_community_vector_adapter_weaviate` directory, replace 
the following lines in the constructor: 

```
self.client = weaviate.use_async_with_weaviate_cloud(
    cluster_url=url,
    auth_credentials=weaviate.auth.AuthApiKey(api_key),
    additional_config=wvc.init.AdditionalConfig(timeout=wvc.init.Timeout(init=30)),
)
```
with the following:

```
self.client = weaviate.use_async_with_local(
    host="localhost",
    port=8080,
    grpc_port=50051
)
```

You can use the docker command provided by Weaviate (https://docs.weaviate.io/deploy/installation-guides/docker-installation)
to run Weaviate with default settings. The command looks something like this, specifying the ports for connection:

```bash
docker run -p 8080:8080 -p 50051:50051 cr.weaviate.io/semitechnologies/weaviate:1.32.4
```

## Usage

```python
import asyncio
import os
from cognee import config, prune, add, cognify, search, SearchType

# Import the register module to enable Weaviate support
import cognee_community_vector_adapter_weaviate.register


async def main():
    # Configure databases
    config.set_relational_db_config(
        {
            "db_provider": "sqlite",
        }
    )
    config.set_vector_db_config(
        {
            "vector_db_provider": "weaviate",
            "vector_db_url": os.getenv("VECTOR_DB_URL"),  # or your Weaviate URL
            "vector_db_key": os.getenv("VECTOR_DB_KEY"),  # or your API key
        }
    )
    config.set_graph_db_config(
        {
            "graph_database_provider": "networkx",
        }
    )

    # Optional: Clean previous data
    await prune.prune_data()
    await prune.prune_system()

    # Add and process your content
    text = "Your text content here"
    await add(text)
    await cognify()

    # Search
    search_results = await search(
        query_type=SearchType.GRAPH_COMPLETION, query_text="Your search query"
    )

    for result in search_results:
        print(result)


if __name__ == "__main__":
    asyncio.run(main())
```

## Configuration

The Weaviate adapter requires the following configuration parameters:

- `vector_db_url`: Your Weaviate cluster endpoint URL
- `vector_db_key`: Your Weaviate API key
- `vector_db_provider`: Set to "weaviate"

### Environment Variables

Set the following environment variables or pass them directly in the config:

```bash
export VECTOR_DB_URL="https://your-weaviate-instance.weaviate.network"
export VECTOR_DB_KEY="your-api-key"
```

**Alternative:** You can also use the [`.env.template`](https://github.com/topoteretes/cognee/blob/main/.env.template) file from the main cognee repository. Copy it to your project directory, rename it to `.env`, and fill in your Weaviate configuration values.

## Requirements

- Python >= 3.11, <= 3.13
- weaviate-client >= 4.9.6, < 5.0.0
- cognee == 1.4.2

## Features

- Full vector search capabilities
- Batch operations support
- Async/await support
- Retry logic for better reliability
- Collection management
- Data point indexing and retrieval 