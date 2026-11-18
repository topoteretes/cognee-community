# cognee-community-connector-slab

A Slab knowledge base data-source connector for [cognee](https://github.com/topoteretes/cognee):
synchronize team wiki posts, topics, and discussions into memory.

Exposes a DLT resource directly compatible with `cognee.remember(...)` and `cognee.add(...)`.
Slab posts are rendered into structured Markdown and ingested as **documents** (routed through
Cognee's cognify entity extraction and knowledge graph construction).

## Features

- **GraphQL Ingestion**: Queries Slab's GraphQL API (`/v1/graphql`) to extract posts, topics, and threaded comments.
- **Relay Cursor Pagination**: Robust traversal over Relay-style connection cursors without infinite loops.
- **Deduplication**: Automatically deduplicates posts across multiple queried topics.
- **Incremental Sync**: Uses post `updatedAt` timestamps stored in DLT state to ingest only new or modified posts.
- **Forget-on-Delete Tombstoning**: Emits `{"id": ..., "_deleted": True}` tombstones when posts are deleted or unshared upstream, safely pruning orphaned graph entities and vector index points from Cognee memory.
- **Edge-Case Resilience**: Cleanly formats missing topics, deactivated comment authors, and empty comments; preserves embedded markdown code blocks and backticks verbatim.
- **Automatic Retries**: Exponential backoff and jitter on rate limits (HTTP 429) and transient network errors.

## Installation

```bash
uv pip install cognee-community-connector-slab
# or within the monorepo:
cd packages/connector/slab && uv sync
```

## Setup

1. Generate a Slab Developer API Token:
   - Navigate to Slab **Settings** -> **Integrations** -> **Developer API**.
   - Create a new token with read access to posts and topics.
2. Set your environment variables:
   ```bash
   export SLAB_API_TOKEN="your_slab_token"
   export LLM_API_KEY="sk-..."
   ```

## Usage

```python
import cognee
from cognee_community_connector_slab import slab_source

# Connect and ingest all visible posts (or pass topic_ids=['...'])
source = slab_source()

await cognee.remember(
    source,
    dataset_name="slab_knowledge",
)

# Search across synced posts
results = await cognee.search(
    query_text="What are our engineering architecture guidelines?",
    query_type=cognee.SearchType.GRAPH_COMPLETION,
    datasets=["slab_knowledge"],
)
print(results)
```

## Testing

Run the full offline test suite (no live Slab token needed):

```bash
cd packages/connector/slab
uv run pytest tests/
```

