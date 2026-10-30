# Cognee Community TuringDB Graph Adapter

This is a community-maintained adapter that enables Cognee to work with TuringDB as a graph database.

## Installation

```bash
pip install cognee-community-graph-adapter-turingdb
```

## Usage

```python
import asyncio
import os
import pathlib
from os import path
from cognee import config, prune, add, cognify, search, SearchType

# Import the register module to enable TuringDB support
from cognee_community_graph_adapter_turingdb import register


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

    # Configure TuringDB as the graph database
    config.set_graph_db_config(
        {
            "graph_database_provider": "turingdb",
            "graph_database_url": os.getenv("GRAPH_DB_URL", "http://localhost:6666"),
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

The TuringDB adapter can be configured as a graph database. The following configuration parameters are available:

**Database Configuration:**
- `graph_database_provider`: Set to "turingdb"
- `graph_database_url`: Your TuringDB server url (default: "http://localhost:6666")

### Environment Variables

Set the following environment variables or pass them directly in the config, or set the in the .env file:

```bash
export GRAPH_DB_URL="http://localhost:6666"
```

**Alternative:** You can also use the [`.env.template`](https://github.com/topoteretes/cognee/blob/main/.env.template) file from the main cognee repository. Copy it to your project directory, rename it to `.env`, and fill in your TuringDB configuration values.

## Requirements

- Python >= 3.11, <= 3.13
- turingdb >= 1.22.0
- cognee >= 0.5.2

This adapter allows Cognee to leverage TuringDB's capabilities for advanced knowledge graph operations.
