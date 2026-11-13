# cognee-community-connector-prisma

A **Prisma ORM schema (`schema.prisma`)** data-source connector for [cognee](https://github.com/topoteretes/cognee): turn your database models, foreign key relations, field types, and enums into structured memory for AI coding agents.

It parses Prisma schema files (models, scalar fields, cross-model relations, composite keys, and enums) and exposes a `dlt` resource you pass directly to `cognee.remember(...)`. It integrates cleanly with cognee's document ingestion pipeline, providing **snapshot sync** and **forget-on-delete** with zero core changes.

## Why This Matters for AI Coding Agents

AI developer assistants (e.g., Cursor, Devin, Copilot) regularly hallucinate column names, missing foreign key relations, and enum values when generating SQL migrations or application code.

By ingesting `schema.prisma` into Cognee:
- Agents know the **exact database models, field types, and constraints** (`@id`, `@unique`, `@default`).
- Cross-model relations (`@relation(fields: [authorId], references: [id])`) are structured into the knowledge graph.
- Documentation comments (`///`) are preserved as entity descriptions.
- Models removed from the schema are automatically purged from Cognee's vector and graph memory via orphan cleanup.

## Installation

```bash
uv pip install cognee-community-connector-prisma
# or from this monorepo:
cd packages/connector/prisma && uv sync --all-extras
```

## Quick Start

```python
import cognee
from cognee_community_connector_prisma import prisma_source

# 1. Ingest schema snapshot into agent memory
await cognee.remember(
    prisma_source("prisma/schema.prisma"),
    dataset_name="database_schema",
    max_rows_per_table=0,  # ingest the full schema
)

# 2. Build graph and semantic indices
await cognee.cognify()

# 3. Ask your database questions!
answer = await cognee.search(
    query_text="How is the Order model related to User, and what fields are required?",
    query_type=cognee.SearchType.GRAPH_COMPLETION,
    datasets=["database_schema"],
)
print(answer)
```

See `examples/example.py` for a runnable demo using sample fixtures in `examples/test_data/`.

## Architecture & Guarantees

* **Stable Primary Keys**: Entities are assigned deterministic IDs:
  `prisma:model:{model_name}` and `prisma:enum:{enum_name}`. Re-syncing unchanged schemas avoids duplicate processing.
* **Snapshot Sync (`replace`)**: Uses `write_disposition="replace"`, matching Cognee's dlt defaults. When a model or field is removed from `schema.prisma`, cognee's orphan cleanup purges it from relational, graph, and vector stores.
* **Relational Graph Modeling**: Translates Prisma `@relation` directives into explicit relational summaries detailing foreign keys and referenced tables.
* **Zero External Cloud Dependencies**: Operates locally on `.prisma` files with zero API tokens or rate limits.

## Testing

Run unit tests locally with `pytest`:

```bash
pytest packages/connector/prisma/tests
```
