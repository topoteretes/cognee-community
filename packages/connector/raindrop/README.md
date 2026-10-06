# cognee-community-connector-raindrop

A Raindrop.io data-source connector for [cognee](https://github.com/topoteretes/cognee):
bring your bookmarks into memory and query them with Cognee.

## Why this connector

Raindrop is a useful source of personal knowledge: saved articles, research papers,
blog posts, docs, and bookmarks often contain the context people want to reason over.
This connector ingests that saved material as normal document-like records so it can be
searched and connected through Cognee.

## Install

```bash
uv pip install cognee-community-connector-raindrop
# or, from this monorepo:
cd packages/connector/raindrop && uv sync --all-extras
```

## Usage

```python
import os

import cognee
from cognee_community_connector_raindrop import raindrop_source

os.environ["RAINDROP_API_TOKEN"] = "<your-token>"

await cognee.remember(
    raindrop_source(),
    dataset_name="raindrop",
)

answer = await cognee.search(
    query_text="What research about memory graphs did I save?",
    query_type=cognee.SearchType.GRAPH_COMPLETION,
    datasets=["raindrop"],
)
```

You can also scope the source with a collection ID or free-text search:

```python
source = raindrop_source(collection_id=123456, search="retrieval")
```

## Setup

1. Create a Raindrop.io app or generate an API token from your account settings.
2. Set `RAINDROP_API_TOKEN` in your environment or pass `token=...` directly.
3. Set `LLM_API_KEY` as needed for normal Cognee ingestion.

## Testing

```bash
uv run pytest tests/
```
