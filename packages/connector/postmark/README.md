# cognee-community-connector-postmark

A Postmark data-source connector for [cognee](https://github.com/topoteretes/cognee):
sync your Postmark transactional email templates and notification messages into searchable AI memory — "enable AI agents to recall and reason over customer emails and notification templates".

It exposes a `dlt` source you pass directly to `cognee.remember(...)` / `cognee.add(...)`. Templates
(subjects, versions, HTML/plain text bodies, aliases, and layouts) are rendered to clean Markdown and
ingested as **normal documents** (flowing through cognee's cognify entity-extraction pipeline, not
the flat relational row path), via cognee's document-mode marker.

---

## Features

- **Full-Snapshot Sync with Forget-on-Delete**: Templates deleted or archived in Postmark are automatically pruned from cognee memory on the next sync (`write_disposition="replace"`).
- **Prose & Knowledge Extraction**: Enables LLM entity extraction and knowledge graph construction across email body content, layouts, and subject lines.
- **Deterministic Deduplication**: Items maintain stable content-hashed IDs so unchanged items are never re-cognified.
- **Granular Scoping**: Ingest by specific Template IDs or Aliases (`template_ids=[...]`) and filter by `template_type`.

---

## Installation

```bash
uv pip install cognee-community-connector-postmark
# or within this monorepo:
cd packages/connector/postmark && uv sync --all-extras
```

---

## Setup & Server Token

1. Log in to [Postmark](https://account.postmarkapp.com).
2. Choose your Server $\rightarrow$ navigate to **API Tokens**.
3. Copy your **Server API Token**.

---

## Quickstart

```python
import asyncio
import os
import cognee
from cognee_community_connector_postmark import postmark_source


async def main():
    # Provide token explicitly or set POSTMARK_SERVER_TOKEN environment variable
    source = postmark_source(server_token=os.environ.get("POSTMARK_SERVER_TOKEN"))

    # Ingest templates into a dedicated dataset
    await cognee.remember(source, dataset_name="postmark", self_improvement=False)

    # Search with natural language
    results = await cognee.search(
        query_text="What transactional email templates do we send for receipts and password resets?",
        query_type=cognee.SearchType.GRAPH_COMPLETION,
        datasets=["postmark"],
    )
    print("Search Result:\n", results)


if __name__ == "__main__":
    asyncio.run(main())
```

---

## Running Tests

Run the test suite with `pytest`:

```bash
pytest packages/connector/postmark/tests
```
