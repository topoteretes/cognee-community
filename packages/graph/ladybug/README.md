# Cognee Community Graph Adapter - Ladybug

Use the Ladybug embedded graph database (the successor to Kùzu) as Cognee's graph backend.
This adapter reuses Cognee's built-in Kùzu integration but transparently swaps in the
[`real_ladybug`](https://pypi.org/project/real_ladybug/) Python bindings so you can
keep using Cognee without the deprecated `kuzu` package.

## Installation

```bash
pip install cognee-community-graph-adapter-ladybug real_ladybug
```

> **Tip:** The adapter automatically falls back to the default Kùzu driver if
> `real_ladybug` is not installed, so you can experiment without removing an
> existing Kùzu environment.

## Usage

```python
import asyncio
import cognee
from cognee_community_graph_adapter_ladybug import register

async def main():
    register()  # make the adapter discoverable

    cognee.config.set_graph_database_provider("ladybug")
    cognee.config.set_graph_db_config({
        # Path to the Ladybug database file (can be absolute, relative, or S3)
        "graph_database_url": ".data_storage/cognee_graph_ladybug",
    })

    await cognee.prune.prune_data()
    await cognee.prune.prune_system()

    documents = [
        "Ladybug is an embedded graph database designed for analytics.",
        "It started as the Kùzu project and is optimized for reasoning workloads.",
    ]

    await cognee.add(documents, dataset_name="ladybug_docs")
    await cognee.cognify(["ladybug_docs"])

    results = await cognee.search(
        query_type=cognee.SearchType.GRAPH_COMPLETION,
        query_text="What is Ladybug?",
    )
    print(results)

if __name__ == "__main__":
    asyncio.run(main())
```

### Configuration Notes

- `graph_database_url` is treated as the Ladybug database path. If omitted, Cognee's
  default `graph_file_path` for the current environment is used instead.
- Remote/S3 paths work because the adapter delegates to Cognee's existing Kùzu
  implementation, including migrations, locking, and S3 helpers.
- All other Cognee graph APIs (`get_graph_engine`, `visualize_graph`, etc.) keep
  working as they rely on the shared `GraphDBInterface`.

## Examples

See [`examples/example.py`](examples/example.py) for a complete runnable workflow
covering registration, ingestion, cognition, and querying.

### Multi-user permissions

Cognee's backend access control feature assigns a dedicated graph/vector database per user
and dataset. To test that flow with Ladybug:

```bash
export ENABLE_BACKEND_ACCESS_CONTROL=True
export OPENAI_API_KEY=sk-...
export LLM_API_KEY=$OPENAI_API_KEY

PYTHONPATH=packages/graph/ladybug \
    python packages/graph/ladybug/examples/multi_user_permissions.py
```

The script provisions two users, grants one of them read access to the other user's dataset,
and proves that permissions are enforced before the grant and respected afterward.

## Requirements

- Python >= 3.10, <= 3.13
- `cognee` >= 0.3.9
- `real_ladybug` >= 0.0.1

## License

MIT, following the Cognee Community project conventions.
