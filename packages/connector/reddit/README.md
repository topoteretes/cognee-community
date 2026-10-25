# Reddit Connector for Cognee

A `dlt` data-source connector that ingests Reddit submissions and discussion comment trees into Cognee memory.

Part of the **Cognee Mergetober Hackathon** (`#4811`).

---

## Features

- **Ingestion:** Fetches posts and nested comment trees from targeted subreddits or specific submission IDs.
- **Full Snapshot & Forget-on-Delete:** Uses `write_disposition="replace"` so that posts or comments deleted upstream naturally drop out of Cognee's graph during orphan cleanup.
- **Recursive Comment Threading:** Renders multi-level discussion threads into formatted markdown documents with author attribution, scores, and depth guards.
- **Document Mode:** Tags resources with `DOCUMENT_SOURCE_ATTR` to route directly through Cognee's `cognify` entity-extraction pipeline.
- **Flexible Auth:** Supports Reddit Script Application OAuth2 as well as public JSON endpoints with custom `User-Agent`.

---

## Installation

Inside your environment:

```bash
pip install cognee-community-connector-reddit
```

Or from source:

```bash
pip install -e packages/connector/reddit
```

---

## Quickstart

```python
import asyncio
import cognee
from cognee_community_connector_reddit import reddit_source

async def main():
    # 1. Initialize Reddit source for targeted subreddits
    source = reddit_source(
        subreddits=["python", "artificial"],
        listing="hot",
        limit=10,
        max_comment_depth=3,
    )

    # 2. Ingest into Cognee memory
    await cognee.add(source)

    # 3. Cognify into knowledge graph
    await cognee.cognify()

    # 4. Search over ingested Reddit discussions
    results = await cognee.search("What are the latest AI frameworks discussed?")
    for res in results:
        print("-", res)

if __name__ == "__main__":
    asyncio.run(main())
```

---

## Authentication Configuration

For authenticated access (higher rate limits), provide credentials via parameters or environment variables:

```bash
export REDDIT_CLIENT_ID="your_client_id"
export REDDIT_CLIENT_SECRET="your_client_secret"
export REDDIT_USERNAME="your_reddit_username"
export REDDIT_PASSWORD="your_reddit_password"
export REDDIT_USER_AGENT="my_app/1.0 by your_username"
```

---

## Running Tests

Run the test suite with `pytest`:

```bash
pytest packages/connector/reddit/tests
```
