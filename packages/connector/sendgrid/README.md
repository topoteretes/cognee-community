# cognee-community-connector-sendgrid

A SendGrid data-source connector for [cognee](https://github.com/topoteretes/cognee):
sync your SendGrid dynamic templates, subject lines, and marketing lists into searchable AI memory — "enable AI agents to recall and reason over customer communications".

It exposes a `dlt` source you pass directly to `cognee.remember(...)` / `cognee.add(...)`. Templates
and contact segments are rendered to clean Markdown and ingested as **normal documents**
(flowing through cognee's cognify entity-extraction pipeline, not the flat relational row path),
via cognee's document-mode marker.

---

## Features

- **Full-Snapshot Sync with Forget-on-Delete**: Templates or lists deleted upstream in SendGrid are automatically pruned from cognee memory on the next sync (`write_disposition="replace"`).
- **Prose & Knowledge Extraction**: Enables LLM entity extraction and knowledge graph construction across email body content, versions, and subject lines.
- **Deterministic Deduplication**: Items maintain stable content-hashed IDs so unchanged items are never re-cognified.
- **Granular Scoping**: Ingest by specific Template IDs (`template_ids=[...]`) and optionally toggle marketing list ingestion (`include_marketing_lists=True`).

---

## Installation

```bash
uv pip install cognee-community-connector-sendgrid
# or within this monorepo:
cd packages/connector/sendgrid && uv sync --all-extras
```

---

## Setup & API Key

1. Log in to [SendGrid](https://app.sendgrid.com).
2. Navigate to **Settings** $\rightarrow$ **API Keys** $\rightarrow$ **Create API Key**.
3. Grant **Read Access** to **Templates** and **Marketing**.
4. Copy your key (starts with `SG...`).

---

## Quickstart

```python
import asyncio
import os
import cognee
from cognee_community_connector_sendgrid import sendgrid_source


async def main():
    # Provide key explicitly or set SENDGRID_API_KEY environment variable
    source = sendgrid_source(api_key=os.environ.get("SENDGRID_API_KEY"))

    # Ingest templates and marketing lists into a dedicated dataset
    await cognee.remember(source, dataset_name="sendgrid", self_improvement=False)

    # Search with natural language
    results = await cognee.search(
        query_text="What email templates do we send for user onboarding, and what are their subjects?",
        query_type=cognee.SearchType.GRAPH_COMPLETION,
        datasets=["sendgrid"],
    )
    print("Search Result:\n", results)


if __name__ == "__main__":
    asyncio.run(main())
```

---

## Running Tests

Run the test suite with `pytest`:

```bash
pytest packages/connector/sendgrid/tests
```
