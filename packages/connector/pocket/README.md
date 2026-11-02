# cognee-community-connector-pocket

A [Pocket](https://getpocket.com) reading list and saved article data-source connector for [cognee](https://github.com/topoteretes/cognee): turn your curated articles, essays, and research bookmarks into long-term agent memory.

## Features

- **Dual Ingestion Paths:**
  - **Live v3 API:** Connects via standard Pocket OAuth credentials with incremental cursor support (`since` timestamp).
  - **Offline HTML Export (`ril_export.html`):** Zero-credential ingestion from Pocket's canonical export archive (`Settings → Export`).
- **Rich Document Extraction:** Preserves origin URLs, author metadata, user tags, reading status (unread vs archived), word counts, and article excerpts.
- **Document-Mode Routing:** Automatically tags resources with `DOCUMENT_SOURCE_ATTR = "pocket"` so articles flow directly into cognee's text-document cognify pipeline.
- **Forget-on-Delete & Tombstones:** Emits `_deleted` boolean markers with `write_disposition="merge"` so purged articles sync out of memory cleanly during orphan cleanup.

## Installation

```bash
uv pip install cognee-community-connector-pocket
# or, from this monorepo:
cd packages/connector/pocket && uv sync --all-extras
```

## Quick Start

### 1. Zero-Credential Offline Ingestion (HTML Export)

```python
import asyncio
import cognee
from cognee_community_connector_pocket import pocket_export_source

async def main():
    await cognee.remember(
        pocket_export_source("ril_export.html"),
        dataset_name="my_reading_list",
        write_disposition="merge",
        max_rows_per_table=0,
    )

    answer = await cognee.search(
        query_text="What articles did I bookmark regarding AI agent architectures?",
        query_type=cognee.SearchType.GRAPH_COMPLETION,
        datasets=["my_reading_list"],
    )
    print(answer)

if __name__ == "__main__":
    asyncio.run(main())
```

### 2. Live API Ingestion

```python
import asyncio
import cognee
from cognee_community_connector_pocket import pocket_source

async def main():
    await cognee.remember(
        pocket_source(
            consumer_key="YOUR_CONSUMER_KEY",
            access_token="YOUR_ACCESS_TOKEN",
            state="all",
        ),
        dataset_name="my_reading_list",
        write_disposition="merge",
        max_rows_per_table=0,
    )

if __name__ == "__main__":
    asyncio.run(main())
```

## Running Tests

```bash
pytest packages/connector/pocket/tests/
```
