# cognee-community-connector-umami

An Umami data-source connector for [cognee](https://github.com/topoteretes/cognee):
sync your Umami website traffic trends, pageview engagement metrics, visitor paths, and bounce rates into searchable AI memory — "enable AI agents to recall and reason over product adoption and traffic patterns".

It exposes a `dlt` source you pass directly to `cognee.remember(...)` / `cognee.add(...)`. Website
traffic summaries, unique visitor trends, bounce rates, and top paths are rendered to clean Markdown
and ingested as **normal documents** (flowing through cognee's cognify entity-extraction pipeline, not
the flat relational row path), via cognee's document-mode marker.

---

## Features

- **Full-Snapshot Sync with Forget-on-Delete**: Websites removed from Umami are automatically pruned from cognee memory on the next sync (`write_disposition="replace"`).
- **Prose & Knowledge Extraction**: Enables LLM entity extraction and knowledge graph construction across website metrics, visitor patterns, and top active pages.
- **Deterministic Deduplication**: Items maintain stable content-hashed IDs so unchanged items are never re-cognified.
- **Granular Scoping**: Ingest by specific website IDs (`website_ids=[...]`) and configure custom endpoints (`base_url`) for self-hosted instances.

---

## Installation

```bash
uv pip install cognee-community-connector-umami
# or within this monorepo:
cd packages/connector/umami && uv sync --all-extras
```

---

## Setup & API Key

1. Log in to your [Umami Cloud](https://cloud.umami.is) or self-hosted dashboard.
2. Navigate to **Settings** $\rightarrow$ **API Keys**.
3. Create an API Key and copy the token.

---

## Quickstart

```python
import asyncio
import os
import cognee
from cognee_community_connector_umami import umami_source

async def main():
    # Provide token explicitly or set UMAMI_API_KEY environment variable
    source = umami_source(
        api_key=os.environ.get("UMAMI_API_KEY"),
        base_url=os.environ.get("UMAMI_BASE_URL", "https://api.umami.is/v1"),
    )

    # Ingest website traffic analytics into a dedicated dataset
    await cognee.remember(source, dataset_name="umami", self_improvement=False)

    # Search with natural language
    results = await cognee.search(
        query_text="What are our top visited website pages and what is the bounce rate trend?",
        query_type=cognee.SearchType.GRAPH_COMPLETION,
        datasets=["umami"],
    )
    print("Search Result:\n", results)

if __name__ == "__main__":
    asyncio.run(main())
```

---

## Running Tests

Run the test suite with `pytest`:

```bash
pytest packages/connector/umami/tests
```
