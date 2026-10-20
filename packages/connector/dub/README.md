# cognee-community-connector-dub

A Dub.co data-source connector for [cognee](https://github.com/topoteretes/cognee):
sync your Dub.co short links, campaign redirects, and click analytics into searchable AI memory — "enable AI agents to recall and reason over marketing URLs and conversion funnels".

It exposes a `dlt` source you pass directly to `cognee.remember(...)` / `cognee.add(...)`. Short links,
destination URLs, tags, and click counts are rendered to clean Markdown and ingested as
**normal documents** (flowing through cognee's cognify entity-extraction pipeline, not the flat
relational row path), via cognee's document-mode marker.

---

## Features

- **Full-Snapshot Sync with Forget-on-Delete**: Links deleted or archived in Dub.co are automatically pruned from cognee memory on the next sync (`write_disposition="replace"`).
- **Prose & Knowledge Extraction**: Enables LLM entity extraction and knowledge graph construction across link destinations, campaign tags, and conversion metrics.
- **Deterministic Deduplication**: Items maintain stable content-hashed IDs so unchanged items are never re-cognified.
- **Granular Scoping**: Ingest by specific tag IDs (`tag_ids=[...]`) and workspace (`workspace_id`).

---

## Installation

```bash
uv pip install cognee-community-connector-dub
# or within this monorepo:
cd packages/connector/dub && uv sync --all-extras
```

---

## Setup & API Key

1. Log in to [Dub.co](https://app.dub.co).
2. Navigate to **Workspace Settings** $\rightarrow$ **API Keys**.
3. Create an API Key and copy the token.

---

## Quickstart

```python
import asyncio
import os
import cognee
from cognee_community_connector_dub import dub_source

async def main():
    # Provide token explicitly or set DUB_API_KEY environment variable
    source = dub_source(api_key=os.environ.get("DUB_API_KEY"))

    # Ingest short links into a dedicated dataset
    await cognee.remember(source, dataset_name="dub", self_improvement=False)

    # Search with natural language
    results = await cognee.search(
        query_text="What campaign short links do we have for product releases, and how many clicks did they receive?",
        query_type=cognee.SearchType.GRAPH_COMPLETION,
        datasets=["dub"],
    )
    print("Search Result:\n", results)

if __name__ == "__main__":
    asyncio.run(main())
```

---

## Running Tests

Run the test suite with `pytest`:

```bash
pytest packages/connector/dub/tests
```
