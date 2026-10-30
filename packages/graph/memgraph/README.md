# Cognee Community Graph Adapter - Memgraph

This package provides a Memgraph graph database adapter for the Cognee framework.

## Installation

```bash
pip install cognee-community-graph-adapter-memgraph
```

## Usage

```python
import asyncio
import cognee
from cognee.infrastructure.databases.graph import get_graph_engine
from cognee_community_graph_adapter_memgraph import register
import pathlib
import os
import pprint


async def main():
    # Register the Memgraph adapter
    register()

    # Configure cognee to use Memgraph
    cognee.config.set_graph_database_provider("memgraph")

    # Set up your Memgraph connection
    cognee.config.set_graph_db_config(
        {
            "graph_database_url": "bolt://localhost:7687",
            "graph_database_username": "memgraph",
            "graph_database_password": "memgraph",
        }
    )

    # Optional: Set custom data and system directories
    system_path = pathlib.Path(__file__).parent
    cognee.config.system_root_directory(os.path.join(system_path, ".cognee_system"))
    cognee.config.data_root_directory(os.path.join(system_path, ".data_storage"))

    # Sample data to add to the knowledge graph
    sample_data = [
        "Artificial intelligence is a branch of computer science that aims to create intelligent machines.",
        "Machine learning is a subset of AI that focuses on algorithms that can learn from data.",
        "Deep learning is a subset of machine learning that uses neural networks with many layers.",
        "Natural language processing enables computers to understand and process human language.",
        "Computer vision allows machines to interpret and make decisions based on visual information.",
    ]

    try:
        print("Adding data to Cognee...")
        await cognee.add(sample_data, "ai_knowledge")

        print("Processing data with Cognee...")
        await cognee.cognify(["ai_knowledge"])

        print("Searching for insights...")
        search_results = await cognee.search(
            query_type=cognee.SearchType.GRAPH_COMPLETION, query_text="artificial intelligence"
        )

        print(f"Found {len(search_results)} insights:")
        for i, result in enumerate(search_results, 1):
            print(f"{i}. {result}")

        print("\nSearching with Chain of Thought reasoning...")
        await cognee.search(
            query_type=cognee.SearchType.GRAPH_COMPLETION_COT,
            query_text="How does machine learning relate to artificial intelligence and what are its applications?",
        )

        print("\nYou can get the graph data directly, or visualize it in an HTML file like below:")

        # Get graph data directly
        graph_engine = await get_graph_engine()
        graph_data = await graph_engine.get_graph_data()

        print("\nDirect graph data:")
        pprint.pprint(graph_data)

        # Or visualize it in HTML
        print("\nVisualizing the graph...")
        await cognee.visualize_graph(system_path / "graph.html")
        print(f"Graph visualization saved to {system_path / 'graph.html'}")

    except Exception as e:
        print(f"Error: {e}")
        print("Make sure Memgraph is running and accessible at bolt://localhost:7687")


if __name__ == "__main__":
    asyncio.run(main())
```

## Requirements

- Python >= 3.10, <= 3.13
- Memgraph database instance
- neo4j driver (for Bolt protocol support)

## Configuration

The adapter requires the following configuration using the `set_graph_db_config()` method:

```python
cognee.config.set_graph_db_config(
    {
        "graph_database_url": "bolt://localhost:7687",  # Memgraph database URL
        "graph_database_username": "memgraph",  # Username for authentication
        "graph_database_password": "memgraph",  # Password for authentication
    }
)
```

### Environment Variables

Set the following environment variables or pass them directly in the config:

```bash
export GRAPH_DATABASE_URL="bolt://localhost:7687"
export GRAPH_DATABASE_USERNAME="memgraph"
export GRAPH_DATABASE_PASSWORD="memgraph"
```

**Alternative:** You can also use the [`.env.template`](https://github.com/topoteretes/cognee/blob/main/.env.template) file from the main cognee repository. Copy it to your project directory, rename it to `.env`, and fill in your Memgraph configuration values.

### Optional Configuration

You can also set custom directories for system and data storage:

```python
cognee.config.system_root_directory("/path/to/system")
cognee.config.data_root_directory("/path/to/data")
```

## Features

- Full support for Memgraph's property graph model
- Optimized queries for graph operations
- Async/await support
- Transaction support
- Comprehensive error handling
- Advanced search functionality:
  - Graph completion search
  - Chain of Thought (COT) reasoning
- Direct graph data access via `get_graph_engine()`
- HTML graph visualization with `cognee.visualize_graph()`
- Custom directory configuration

## Example

See `example.py` for a complete working example that demonstrates:
- Setting up the Memgraph adapter
- Adding comprehensive AI/ML knowledge to the graph
- Processing data with cognee
- Searching with graph completion
- Chain of Thought reasoning searches
- Direct graph data access and inspection
- HTML graph visualization
- Comprehensive error handling

## Contributing

Contributions are welcome! Please feel free to submit a Pull Request.

## License

This project is licensed under the MIT License. 