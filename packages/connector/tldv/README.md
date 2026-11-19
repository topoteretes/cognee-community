# Cognee tl;dv Connector

A `dlt`-backed data-source connector for [tl;dv](https://tldv.io) meeting recordings, AI summaries, action items, and transcripts into Cognee's entity knowledge graph.

## Installation

```bash
pip install cognee-community-connector-tldv
```

## Quickstart

```python
import asyncio
import cognee
from cognee_community_connector_tldv import tldv_source


async def main():
    source = tldv_source(
        api_key="YOUR_TLDV_API_KEY",
        limit=50,
    )

    await cognee.add(source, dataset_name="meetings_memory")
    await cognee.cognify(dataset_name="meetings_memory")

    results = await cognee.search(
        "What are the upcoming deliverables mentioned in the sprint review?",
        dataset_name="meetings_memory",
    )
    print(results)


if __name__ == "__main__":
    asyncio.run(main())
```

## Features
- **Prose Document Transformation**: Converts multi-speaker dialogue, timestamps, and AI meeting summaries into rich documents recognized by Cognee's NLP graph extraction (`DOCUMENT_SOURCE_ATTR = "tldv"`).
- **Pagination & Filtering**: Supports date boundary filtering (`from_date`, `to_date`) and automatic pagination.
- **Idempotency**: Configured with `write_disposition="replace"` for seamless synchronizations and lifecycle cleanup.
