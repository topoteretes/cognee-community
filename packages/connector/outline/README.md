# cognee-community-connector-outline

An Outline Knowledge Base data-source connector for [cognee](https://github.com/topoteretes/cognee):
sync your Outline team wikis, engineering runbooks, and internal documents into memory — "ask my Outline wiki".

It exposes a `dlt` source you hand to `cognee.remember(...)` / `cognee.add(...)`. Outline
documents natively carry markdown text and are ingested as **normal documents**
(flowing through Cognee's `cognify` entity-extraction pipeline, not the deterministic dlt-row path),
via Cognee's document-mode marker.

## Requirements

> **This connector requires a cognee release that ships "document-mode"** — i.e.
> `cognee.tasks.ingestion.dlt_utils.DOCUMENT_SOURCE_ATTR` and the `resolve_dlt_sources`
> routing that reads it.

## Install

```bash
uv pip install cognee-community-connector-outline
# or, from this monorepo:
cd packages/connector/outline && uv sync --all-extras
```

## Usage

```python
import cognee
from cognee_community_connector_outline import outline_source

# Connect to Outline Cloud or self-hosted instance
await cognee.remember(
    outline_source(
        base_url="https://app.getoutline.com/api",
        api_token="ol_api_token_here",
        collection_ids=["engineering", "product_specs"],  # Optional: omit to sync all accessible docs
    ),
    dataset_name="outline_wiki",
)

answer = await cognee.search(
    query_text="What are our engineering deployment procedures and runbooks?",
    query_type=cognee.SearchType.GRAPH_COMPLETION,
    datasets=["outline_wiki"],
)
```

## Features

- **Outline Cloud & Self-Hosted**: Supports both `https://app.getoutline.com/api` and self-hosted Outline instances.
- **Native Markdown Extraction**: Outline documents provide clean native Markdown text, which is preserved and indexed into Cognee's entity extraction and knowledge graph pipelines.
- **Collection Scoping**: Supports syncing specific collections or the entire workspace.
- **Sensitive Field Filtering**: Automatically redacts sensitive fields like API tokens, secrets, and passwords from custom metadata.
- **Pagination & Retries**: Auto-paginates Outline results with exponential backoff and rate-limit (`Retry-After`) handling.
- **Full Snapshot & Cleanup**: `write_disposition="replace"` ensures deleted or archived documents are purged by Cognee's `orphan_cleanup`.

## How sync + forget-on-delete work

The source is a **full snapshot**: `write_disposition="replace"` rewrites staging with
the records currently visible in Outline collections on each run. Deleted or archived documents
drop out of the snapshot, allowing Cognee's `orphan_cleanup` to remove them from knowledge graphs
and vector stores.
