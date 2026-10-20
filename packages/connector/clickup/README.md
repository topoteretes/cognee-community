# cognee-community-connector-clickup

A ClickUp data-source connector for [cognee](https://github.com/topoteretes/cognee):
sync your ClickUp workspace into searchable AI memory — "chat with your company tasks and projects".

It exposes a `dlt` source you pass directly to `cognee.remember(...)` / `cognee.add(...)`. ClickUp
tasks (descriptions, status, priority, assignees, checklists, and tags) are rendered to Markdown and
ingested as **normal documents** (flowing through cognee's cognify entity-extraction pipeline, not
the raw relational row path), via cognee's document-mode marker.

---

## Features

- **Full-Snapshot Sync with Forget-on-Delete**: Tasks deleted or archived in ClickUp are automatically pruned from cognee memory on the next sync (`write_disposition="replace"`).
- **Prose & Knowledge Extraction**: Enables LLM entity extraction and knowledge graph construction across task descriptions and checklists.
- **Deterministic Deduplication**: Tasks maintain stable content-hashed IDs so unchanged items are never re-cognified.
- **Granular Scoping**: Ingest by workspace, specific Space IDs (`space_ids=[...]`), or List IDs (`list_ids=[...]`).

---

## Installation

```bash
uv pip install cognee-community-connector-clickup
# or within this monorepo:
cd packages/connector/clickup && uv sync --all-extras
```

---

## Setup & API Token

1. Log in to [ClickUp](https://app.clickup.com).
2. Go to your **Profile Icon** (bottom-left) $\rightarrow$ **Settings** $\rightarrow$ **Apps**.
3. Under **API Token**, click **Generate** and copy your personal token (starts with `pk_...`).

---

## Quickstart

```python
import asyncio
import os
import cognee
from cognee_community_connector_clickup import clickup_source

async def main():
    # Provide token explicitly or set CLICKUP_API_TOKEN environment variable
    source = clickup_source(api_token=os.environ.get("CLICKUP_API_TOKEN"))

    # Ingest tasks into a dedicated dataset
    await cognee.remember(source, dataset_name="clickup", self_improvement=False)

    # Search your tasks with natural language
    results = await cognee.search(
        query_text="What features are planned for the upcoming release and who is leading them?",
        query_type=cognee.SearchType.GRAPH_COMPLETION,
        datasets=["clickup"],
    )
    print("Search Result:\n", results)

if __name__ == "__main__":
    asyncio.run(main())
```

---

## Running Tests

Run the test suite with `pytest`:

```bash
pytest packages/connector/clickup/tests
```
