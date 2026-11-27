# Cognee Community Amazon Neptune Adapter

Community-maintained adapters that let Cognee use
[Amazon Neptune Analytics](https://docs.aws.amazon.com/neptune-analytics/latest/userguide/what-is-neptune-analytics.html)
as a graph store, or as a hybrid graph and vector store on a single graph.

These adapters previously shipped inside `cognee` itself (the `cognee[neptune]`
extra). They now live in this package.

| Provider name       | Class                     | Use as                                    |
|---------------------|---------------------------|-------------------------------------------|
| `neptune`           | `NeptuneGraphDB`          | Graph database                            |
| `neptune_analytics` | `NeptuneAnalyticsAdapter` | Graph database and vector database (both) |

## Installation

```bash
pip install cognee-community-hybrid-adapter-neptune
```

## Prerequisites

Unlike most adapters in this repository, Neptune needs a cloud account rather
than a local server:

1. An AWS account with a
   [provisioned Neptune Analytics graph](https://docs.aws.amazon.com/neptune-analytics/latest/userguide/create-graph-using-console.html).
   To use it as a vector store, the graph's vector search dimension must match
   your embedding model's dimension.
2. AWS credentials authorized to access the graph, available through the
   standard AWS credential chain, for example `AWS_ACCESS_KEY_ID`,
   `AWS_SECRET_ACCESS_KEY` and `AWS_REGION` (plus `AWS_SESSION_TOKEN` for
   temporary credentials).
3. A configured LLM and embedding model for Cognee (`LLM_API_KEY`, ...).

## Configuration

The endpoint has the format `neptune-graph://<GRAPH_ID>`.

Graph and vector on the same Neptune Analytics graph:

```bash
GRAPH_DATABASE_PROVIDER=neptune_analytics
GRAPH_DATABASE_URL=neptune-graph://g-abc1234567
VECTOR_DB_PROVIDER=neptune_analytics
VECTOR_DB_URL=neptune-graph://g-abc1234567
```

Graph only, with another vector store:

```bash
GRAPH_DATABASE_PROVIDER=neptune
GRAPH_DATABASE_URL=neptune-graph://g-abc1234567
```

Neptune has no per-dataset database handler, so it requires
`ENABLE_BACKEND_ACCESS_CONTROL=false`.

## Usage

Import the `register` module before using Cognee. It makes the `neptune` and
`neptune_analytics` providers available.

```python
import asyncio
import os

import cognee
from cognee import SearchType

# Registers the `neptune` and `neptune_analytics` providers with cognee.
from cognee_community_hybrid_adapter_neptune import register  # noqa: F401


async def main():
    endpoint = "neptune-graph://" + os.environ["GRAPH_ID"]

    cognee.config.set_graph_db_config(
        {
            "graph_database_provider": "neptune_analytics",
            "graph_database_url": endpoint,
        }
    )
    cognee.config.set_vector_db_config(
        {
            "vector_db_provider": "neptune_analytics",
            "vector_db_url": endpoint,
        }
    )

    await cognee.add("Neptune Analytics is a memory-optimized graph database engine.")
    await cognee.cognify()

    results = await cognee.search(
        query_type=SearchType.GRAPH_COMPLETION,
        query_text="What is Neptune Analytics?",
    )
    for result in results:
        print(result)


asyncio.run(main())
```

A fuller script is in [`examples/example.py`](examples/example.py).

The adapters can also be constructed directly with a graph identifier:

```python
from cognee_community_hybrid_adapter_neptune import NeptuneGraphDB

graph = NeptuneGraphDB("g-abc1234567", region="us-east-1")
```

## Migrating from `cognee[neptune]`

1. Replace `pip install "cognee[neptune]"` with
   `pip install cognee-community-hybrid-adapter-neptune`.
2. Add `from cognee_community_hybrid_adapter_neptune import register` before
   the first Cognee call.
3. Update direct imports:
   - `cognee.infrastructure.databases.graph.neptune_driver` →
     `cognee_community_hybrid_adapter_neptune`
   - `cognee.infrastructure.databases.hybrid.neptune_analytics.NeptuneAnalyticsAdapter` →
     `cognee_community_hybrid_adapter_neptune.neptune_analytics_adapter`

Provider names, the endpoint format and the stored data layout are unchanged.

## Tests

```bash
poetry install
poetry run pytest tests/unit
```

`tests/unit` runs offline with no AWS access. The scripts directly under
`tests/` (`test_neptune_analytics_graph.py`, `test_neptune_analytics_hybrid.py`,
`test_neptune_analytics_vector.py`) talk to a real Neptune Analytics graph named
by `GRAPH_ID` and **delete its contents**; run them only against a disposable
graph.
