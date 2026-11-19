# Flarum Community Forum Connector for Cognee

A `dlt` data-source connector that ingests discussions, thread categories, reply sequences, and user posts from Flarum forums into Cognee memory for community intelligence and natural language search.

Part of the **Cognee Mergetober Hackathon** (`#6216`).

---

## Features

- **Discussion & Thread Ingestion:** Ingests forum discussions, tags, comment counts, creation dates, and last activity timestamps.
- **Chronological Post Extraction:** Formats multi-post discussion threads into coherent Markdown documents with HTML tag stripping.
- **Tag Filtering:** Filter discussions by category/tag slug (e.g. `general`, `announcements`, `support`).
- **Flexible Auth:** Supports guest/public forum reads as well as authenticated API token requests.
- **Document Mode:** Tags resources with `DOCUMENT_SOURCE_ATTR` to route directly through Cognee's `cognify` entity-extraction pipeline.

---

## Quickstart

```python
import asyncio
import os
import cognee
from cognee_community_connector_flarum import flarum_source

async def main():
    # 1. Initialize Flarum source
    source = flarum_source(
        base_url=os.getenv("FLARUM_BASE_URL", "https://discuss.flarum.org"),
        tags=["general"],
        limit=20,
    )

    # 2. Add to Cognee memory
    await cognee.add(source)

    # 3. Cognify into knowledge graph
    await cognee.cognify()

    # 4. Search forum discussions
    results = await cognee.search("What issues or questions have users reported with extensions?")
    for res in results:
        print("-", res)

if __name__ == "__main__":
    asyncio.run(main())
```

---

## Running Tests

Run the hermetic offline test suite:

```bash
pytest packages/connector/flarum/tests
```
