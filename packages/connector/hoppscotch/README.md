# cognee-community-connector-hoppscotch

A [Hoppscotch](https://hoppscotch.io) **API collections & environments** data-source connector for [cognee](https://github.com/topoteretes/cognee): turn your API collections, endpoints, parameters, and payloads into structured memory for AI agents.

It parses Hoppscotch collections (JSON exports with recursive folder hierarchies, HTTP methods, headers, parameters, auth settings, and request body payloads) and environment variable configurations, exposing a `dlt` resource you pass directly to `cognee.remember(...)`. It integrates cleanly with cognee's document ingestion pipeline, providing **snapshot sync** and **forget-on-delete** with zero core changes.

## Why This Matters for AI Agents

AI coding agents and autonomous developer assistants often need to interact with internal microservices, payment gateways, and backend endpoints. Currently, this knowledge is locked away in API client tools.

By connecting Hoppscotch to Cognee:
- Agents know the **exact endpoints, methods, and parameters** required to perform actions.
- Sample payloads and JSON schemas are indexed in the knowledge graph.
- Environment variables (e.g. `<<baseUrl>>`, `<<apiVersion>>`) are automatically resolved.
- Endpoints removed upstream in Hoppscotch are automatically cleaned up from Cognee's vector and graph stores.

## Installation

```bash
uv pip install cognee-community-connector-hoppscotch
# or from this monorepo:
cd packages/connector/hoppscotch && uv sync --all-extras
```

## Quick Start

```python
import cognee
from cognee_community_connector_hoppscotch import hoppscotch_source

# 1. Ingest collection snapshot into agent memory
await cognee.remember(
    hoppscotch_source(
        collection_path="path/to/hoppscotch-collection.json",
        environment_path="path/to/hoppscotch-environment.json",  # optional
    ),
    dataset_name="api_specs",
    max_rows_per_table=0,  # ingest the full collection
)

# 2. Build graph and semantic indices
await cognee.cognify()

# 3. Ask your API questions!
answer = await cognee.search(
    query_text="What endpoint do I call to refund a charge, and what payload is needed?",
    query_type=cognee.SearchType.GRAPH_COMPLETION,
    datasets=["api_specs"],
)
print(answer)
```

See `examples/example.py` for a complete runnable demo using sample fixtures in `examples/test_data/`.

## Architecture & Guarantees

* **Stable Primary Keys**: Endpoints are given unique, deterministic IDs:
  `hoppscotch:{collection_slug}:{folder_path}:{request_name}`. Re-syncing unchanged endpoints avoids duplicate processing.
* **Snapshot Sync (`replace`)**: Uses `write_disposition="replace"`, matching Cognee's dlt defaults. When an endpoint is removed from the Hoppscotch export, cognee's orphan cleanup purges it from relational, graph, and vector stores.
* **Environment Variable Expansion**: Automatically resolves `<<variable>>` syntax across URLs, headers, and request bodies when an environment file is supplied.
* **Zero Cloud Credentials Required**: Operates on exported JSON archives or workspace directories, ensuring fully reproducible, deterministic execution without third-party API rate limits.

## Testing

Run unit tests locally with `pytest`:

```bash
pytest packages/connector/hoppscotch/tests
```
