# cognee-community-connector-postman

[![Python 3.11+](https://img.shields.io/badge/python-3.11+-blue.svg)](https://www.python.org/)
[![License](https://img.shields.io/badge/license-Apache%202.0-green.svg)](LICENSE)
[![PyPI Version][pypi-badge]][pypi-link]

[pypi-badge]: https://img.shields.io/pypi/v/cognee-community-connector-postman.svg
[pypi-link]: https://pypi.org/project/cognee-community-connector-postman/

A Postman data-source connector for [cognee](https://github.com/topoteretes/cognee):
ingest Postman API collections, folders, and request descriptions into memory for
semantic search and knowledge graph reasoning: "ask my API documentation".

It exposes a `dlt` source passed to `cognee.add(...)` / `cognee.remember(...)`.
Collections and endpoints are rendered into structured markdown documents and
ingested as normal documents through Cognee's `cognify` entity-extraction pipeline
via Cognee's document-mode marker (`DOCUMENT_SOURCE_ATTR = "postman"`).

## Requirements

This connector requires a Cognee release that includes document-mode support
(`cognee>=1.4.0`). Document mode routes documents through `cognify` instead of
relational table loading and cleans up deleted documents via `orphan_cleanup`.

Python support: Python 3.11, 3.12, and 3.13.

## Installation

Install via pip:

```bash
pip install cognee-community-connector-postman
```

Or install with optional dlt extras:

```bash
pip install "cognee-community-connector-postman[dlt]"
# or directly install dlt with sqlalchemy
pip install "dlt[sqlalchemy]>=1.9.0,<2"
```

For development within the monorepo:

```bash
cd packages/connector/postman
uv sync --all-extras
```

## Configuration & Authentication

### 1. Postman API Key

Authenticate using a Postman API key. Generate an API key in Postman under
Account Settings -> API Keys.

Set the key as an environment variable:

```bash
export POSTMAN_API_KEY="PMAK-your-api-key-here"
```

Or pass it directly to `postman_source(api_key="...")`.

### 2. Workspace Filtering

To ingest collections from a specific Postman workspace, provide `workspace_id`:

```python
from cognee_community_connector_postman import postman_source

source = postman_source(workspace_id="12345678-abcd-ef01-2345-6789abcdef01")
```

### 3. Collection Filtering

To ingest specific collections, pass a list of collection IDs or UIDs:

```python
from cognee_community_connector_postman import postman_source

source = postman_source(
    collection_ids=[
        "12345678-1111-2222-3333-444444444444",
        "12345678-5555-6666-7777-888888888888",
    ]
)
```

Omit `collection_ids` to ingest all collections accessible to the API key.

### 4. Rate Limiting and Resilience

The Postman API enforces a rate limit of 300 requests per minute. The connector's
built-in HTTP client automatically handles rate limits and transient errors:
- Catches HTTP 429 (Too Many Requests) and waits according to the `Retry-After` header.
- Applies exponential backoff for transient 5xx server errors (500, 502, 503, 504).
- Retries eligible requests up to 5 times before raising `PostmanRateLimitError`.

## Usage with Cognee

The following example shows end-to-end ingestion, entity extraction, and graph search:

```python
import asyncio
import os
import cognee
from cognee_community_connector_postman import postman_source


async def main() -> None:
    # 1. Initialize Postman source with optional workspace filtering
    source = postman_source(
        api_key=os.environ.get("POSTMAN_API_KEY"),
        workspace_id=os.environ.get("POSTMAN_WORKSPACE_ID"),
    )

    # 2. Add Postman documents to a dedicated dataset
    await cognee.add(source, dataset_name="postman_apis")

    # 3. Extract entities, relationships, and build graph memory
    await cognee.cognify(datasets=["postman_apis"])

    # 4. Perform semantic search across API documentation
    results = await cognee.search(
        query_text="How do I authenticate against the payment service?",
        query_type=cognee.SearchType.GRAPH_COMPLETION,
        datasets=["postman_apis"],
    )
    print("Search Results:", results)


if __name__ == "__main__":
    asyncio.run(main())
```

See `examples/example.py` for a complete runnable demonstration.

## Incremental Sync Architecture

The connector implements a full-snapshot sync pattern with incremental fetch optimization:

### 1. Full Snapshot Replace

The DLT resource runs with `write_disposition="replace"`. Each sync yields the complete
set of active documents for the configured collections, replacing the staging table.

### 2. Incremental Fetch via updatedAt

- The connector calls `GET /collections` to fetch lightweight collection summaries.
- For each collection, it checks the collection's `updatedAt` timestamp against the
  timestamp stored in `dlt.current.resource_state()`.
- Unchanged collections: If `updatedAt` matches the cached timestamp, 0 detail calls
  (`GET /collections/{uid}`) are executed. The connector re-yields cached document rows
  directly from resource state.
- Modified or new collections: If `updatedAt` is newer or not yet cached, the connector
  fetches the full collection schema, renders markdown documents, updates the cache,
  and yields the new rows.

### 3. Upstream Deletion & Orphan Cleanup

- When a collection is deleted or unshared in Postman, it is omitted from `GET /collections`.
- The sync engine detects its absence and evicts it from the resource state cache.
- Because removed documents are omitted from the yielded snapshot, Cognee's
  `orphan_cleanup` process detects the missing items and purges them from the knowledge
  graph, vector index, and relational storage.

### 4. Deterministic Stable Content IDs

Document IDs use stable composite identifiers (`f"{collection_uid}:{item_id}"`) and
UUIDv5 fallbacks for items without IDs. Volatile timestamps (`updatedAt`) are excluded
from document IDs. This guarantees deterministic Cognee `data_id`s, preventing redundant
re-cognification of unchanged endpoints.

## Offline Testing & CI

The connector includes an extensive test suite that runs 100% offline without live Postman
credentials or network calls, using mocked clients and Postman v2.1.0 schema fixtures:

```bash
# Run package unit tests
pytest packages/connector/postman/tests/ -v
```

### Running Specific Test Tiers

```bash
# Tier 1: Feature Coverage (Category-Partition)
pytest packages/connector/postman/tests/test_postman.py -k "TestTier1" -v

# Tier 2: Boundary Value Analysis and Corner Cases
pytest packages/connector/postman/tests/test_postman.py -k "TestTier2" -v

# Tier 3: Pairwise Combinatorial Interactions
pytest packages/connector/postman/tests/test_postman.py -k "TestTier3" -v

# Tier 4: Real-World Production Workloads
pytest packages/connector/postman/tests/test_postman.py -k "TestTier4" -v
```

### Code Formatting and Linting

```bash
ruff check packages/connector/postman/
ruff format packages/connector/postman/
```

## Contributing & DCO Sign-off

Contributions to `cognee-community-connector-postman` are welcome! Please adhere to the
following repository standards:

1. **Developer Certificate of Origin (DCO)**:
   All commits must be signed off conforming to the Topoteretes DCO:
   ```bash
   git commit -s -m "feat(connector): add feature description"
   ```

2. **Formatting & Quality**:
   - 4-space indentation for Python files.
   - Line length strictly limited to 100 characters.
   - Code must pass `ruff check .` and `ruff format .`.
   - Absolute ZERO emdashes (no unicode \u2014 or \u2013) across code, comments,
     and documentation.
   - Use `cognee.shared.logging_utils` for logging.

## License

This project is licensed under the Apache License 2.0.
