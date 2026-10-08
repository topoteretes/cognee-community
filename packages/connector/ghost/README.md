# cognee-community-connector-ghost

A Ghost CMS data-source connector for [cognee](https://github.com/topoteretes/cognee):
sync your Ghost blog, newsletters, posts, and pages into memory — "ask my Ghost publication".

It exposes a `dlt` source you hand to `cognee.remember(...)` / `cognee.add(...)`. Ghost
posts and pages are extracted and ingested as **normal documents** (they flow through
cognee's cognify entity-extraction pipeline, not the deterministic dlt-row path), via
cognee's document-mode marker.

## Requirements

> **This connector requires a cognee release that ships "document-mode"** — i.e.
> `cognee.tasks.ingestion.dlt_utils.DOCUMENT_SOURCE_ATTR` and the `resolve_dlt_sources`
> routing that reads it.

## Install

```bash
uv pip install cognee-community-connector-ghost
# or, from this monorepo:
cd packages/connector/ghost && uv sync --all-extras
```

## Usage

```python
import cognee
from cognee_community_connector_ghost import ghost_source

# Ghost Content API (Content API key)
await cognee.remember(
    ghost_source(
        base_url="https://demo.ghost.io",
        content_api_key="22444f78447824855665e0889e",
        include_posts=True,
        include_pages=True,
    ),
    dataset_name="ghost_publications",
)

answer = await cognee.search(
    query_text="What are our latest product updates and guides?",
    query_type=cognee.SearchType.GRAPH_COMPLETION,
    datasets=["ghost_publications"],
)
```

## Features

- **Ghost Content API v5**: Connects cleanly to Ghost instances using the official Content API key.
- **Document Pipeline Integration**: Posts and pages format titles, authors, tags, and content into rich markdown and flow into Cognee's entity-extraction (`cognify`) pipeline.
- **Filtering**: Supports Ghost NQL filtering (e.g. `tag:engineering+featured:true`).
- **Pagination & Retries**: Auto-paginates Ghost results with exponential backoff and rate-limit handling.
- **Full Snapshot & Cleanup**: `write_disposition="replace"` ensures deleted posts are removed by Cognee's `orphan_cleanup`.

## How sync + forget-on-delete work

The source is a **full snapshot**: `write_disposition="replace"` rewrites staging with
the records currently visible in Ghost on each run. Deleted posts drop out of the snapshot,
allowing Cognee's `orphan_cleanup` to remove them from knowledge graphs and vector stores.
