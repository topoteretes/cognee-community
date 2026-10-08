# cognee-community-connector-appwrite

An Appwrite data-source connector for [cognee](https://github.com/topoteretes/cognee):
sync your Appwrite database collections and documents into memory — "ask my Appwrite databases".

It exposes a `dlt` source you hand to `cognee.remember(...)` / `cognee.add(...)`. Appwrite
documents are transformed into rich markdown documents and ingested as **normal documents**
(flowing through Cognee's `cognify` entity-extraction pipeline, not the deterministic dlt-row path),
via Cognee's document-mode marker.

## Requirements

> **This connector requires a cognee release that ships "document-mode"** — i.e.
> `cognee.tasks.ingestion.dlt_utils.DOCUMENT_SOURCE_ATTR` and the `resolve_dlt_sources`
> routing that reads it.

## Install

```bash
uv pip install cognee-community-connector-appwrite
# or, from this monorepo:
cd packages/connector/appwrite && uv sync --all-extras
```

## Usage

```python
import cognee
from cognee_community_connector_appwrite import appwrite_source

# Connect to Appwrite Cloud or self-hosted instance
await cognee.remember(
    appwrite_source(
        endpoint="https://cloud.appwrite.io/v1",
        project_id="my-project-id",
        api_key="my-server-api-key",
        database_id="main_database",
        collection_ids=["articles", "notes", "knowledge_base"],
    ),
    dataset_name="appwrite_docs",
)

answer = await cognee.search(
    query_text="What are our core architectural guidelines and components?",
    query_type=cognee.SearchType.GRAPH_COMPLETION,
    datasets=["appwrite_docs"],
)
```

## Features

- **Appwrite Cloud & Self-Hosted**: Supports both `https://cloud.appwrite.io/v1` and custom self-hosted endpoints.
- **Document Pipeline Integration**: Documents format titles, custom attributes, timestamps, and body text into rich markdown and flow into Cognee's entity-extraction (`cognify`) pipeline.
- **Query Support**: Supports passing custom Appwrite queries (e.g. `equal("status", "published")`).
- **Sensitive Field Filtering**: Automatically redacts sensitive fields like passwords, hashes, API keys, tokens, and `$permissions`.
- **Pagination & Retries**: Auto-paginates Appwrite results with exponential backoff and rate-limit (`Retry-After`) handling.
- **Full Snapshot & Cleanup**: `write_disposition="replace"` ensures deleted documents are purged by Cognee's `orphan_cleanup`.

## How sync + forget-on-delete work

The source is a **full snapshot**: `write_disposition="replace"` rewrites staging with
the records currently visible in Appwrite collections on each run. Deleted documents drop out
of the snapshot, allowing Cognee's `orphan_cleanup` to remove them from knowledge graphs
and vector stores.
