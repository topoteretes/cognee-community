# cognee-community-connector-graphql

A **GraphQL Schema Definition Language (`schema.graphql`)** data-source connector for [cognee](https://github.com/topoteretes/cognee): turn your GraphQL types, queries, mutations, subscriptions, inputs, and enums into structured memory for AI coding agents.

It parses `.graphql` / `.gql` schema files and exposes a `dlt` resource you pass directly to `cognee.remember(...)`. It integrates cleanly with cognee's document ingestion pipeline, providing **snapshot sync** and **forget-on-delete** with zero core changes.

## Why This Matters for AI Coding Agents

AI developer assistants regularly hallucinate GraphQL field arguments, input types, and return signatures when writing client queries or server resolvers.

By ingesting `schema.graphql` into Cognee:
- Agents know the **exact types, required non-null fields (`!`), and default arguments**.
- Input object shapes (`input CreateUserInput`) and mutation signatures are modeled in the knowledge graph.
- Docstrings (`"""..."""`) are preserved as entity descriptions.
- Deprecated or deleted operations are automatically purged from Cognee's memory via orphan cleanup.

## Installation

```bash
uv pip install cognee-community-connector-graphql
# or from this monorepo:
cd packages/connector/graphql && uv sync --all-extras
```

## Quick Start

```python
import cognee
from cognee_community_connector_graphql import graphql_source

# 1. Ingest GraphQL schema snapshot into agent memory
await cognee.remember(
    graphql_source("schema.graphql"),
    dataset_name="api_schema",
    max_rows_per_table=0,  # ingest the full schema
)

# 2. Build graph and semantic indices
await cognee.cognify()

# 3. Ask your GraphQL questions!
answer = await cognee.search(
    query_text="What are the required input fields for createArticle and what does it return?",
    query_type=cognee.SearchType.GRAPH_COMPLETION,
    datasets=["api_schema"],
)
print(answer)
```

See `examples/example.py` for a runnable demo using sample fixtures in `examples/test_data/`.

## Architecture & Guarantees

* **Stable Primary Keys**: Entities are assigned deterministic IDs:
  `graphql:{kind}:{name}` (e.g. `graphql:type:user`, `graphql:input:createuserinput`). Re-syncing unchanged schemas avoids duplicate processing.
* **Snapshot Sync (`replace`)**: Uses `write_disposition="replace"`, matching Cognee's dlt defaults. When an entity is removed from `schema.graphql`, cognee's orphan cleanup purges it from relational, graph, and vector stores.
* **Zero External Cloud Dependencies**: Operates locally on `.graphql` / `.gql` files with zero API tokens or rate limits.

## Testing

Run unit tests locally with `pytest`:

```bash
pytest packages/connector/graphql/tests
```
