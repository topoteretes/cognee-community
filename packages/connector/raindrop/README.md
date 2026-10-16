# Raindrop.io Connector for cognee

Sync your [Raindrop.io](https://raindrop.io/) bookmarks into cognee's memory graph.

## Features

- **Full-snapshot sync**: Each run replaces the staging with exactly the bookmarks currently in your Raindrop.io account. Deleted bookmarks are automatically forgotten from the graph.
- **Collection filtering**: Optionally restrict sync to specific collections.
- **Notes & tags**: Bookmark notes and tags are included in the document content for better retrieval.
- **Incremental by content hash**: Unchanged bookmarks keep a stable `data_id`, so they are not re-ingested or re-cognified.

## Setup

1. Get a test token from [Raindrop.io Apps](https://app.raindrop.io/#settings/apps)
2. Install the connector:

```bash
pip install cognee-community-connector-raindrop
```

3. Set your token:

```bash
export RAINDROP_TOKEN="your-test-token-here"
```

## Usage

```python
import asyncio
import cognee
from cognee_community_connector_raindrop import raindrop_source

async def main():
    source = raindrop_source()
    # max_rows_per_table=0 is required for full-snapshot sync (default is 50)
    await cognee.add(source, max_rows_per_table=0)

    results = await cognee.search(query_text="machine learning")
    for result in results:
        print(result)

asyncio.run(main())
```

### Collection filtering

```python
# Only sync specific collections
source = raindrop_source(collection_ids=[123456, 789012])
```

## How it works

1. The connector fetches all bookmarks (or from specified collections) via the Raindrop.io REST API.
2. Each bookmark is flattened into a document with title, URL, excerpt, notes, and tags.
3. The source declares `cognee_document_source = "raindrop"`, routing bookmarks through the standard cognify entity-extraction pipeline.
4. Under `write_disposition="replace"`, each full sync replaces the previous snapshot. Deleted bookmarks fall out of staging and cognee's `orphan_cleanup` removes them from the graph.

## Development

```bash
cd packages/connector/raindrop
pip install -e .
pytest tests/
```

## License

MIT
