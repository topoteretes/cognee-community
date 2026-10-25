# Drupal CMS Connector for Cognee

A `dlt` data-source connector that ingests content types (articles, basic pages, policies, documentation) from Drupal core JSON:API into Cognee memory for natural language retrieval and enterprise search.

Part of the **Cognee Mergetober Hackathon** (`#6299`).

---

## Features

- **Drupal Core JSON:API:** Connects directly to Drupal's native `/jsonapi/node/{bundle}` endpoints with zero third-party Drupal modules required.
- **Multiple Bundles:** Ingest articles, pages, or custom content types simultaneously.
- **HTML Sanitization:** Cleans rich HTML body fields into readable Markdown for high-accuracy embedding and entity extraction.
- **Authentication Flexibility:** Supports public read-only content as well as HTTP Basic Authentication for protected/unpublished nodes.
- **Snapshot Replacement:** Configured with `write_disposition="replace"` for clean synchronization.
- **Document Mode:** Tags resources with `DOCUMENT_SOURCE_ATTR` to route directly through Cognee's `cognify` entity-extraction pipeline.

---

## Quickstart

```python
import asyncio
import os
import cognee
from cognee_community_connector_drupal import drupal_source

async def main():
    # 1. Initialize Drupal connector
    source = drupal_source(
        base_url=os.getenv("DRUPAL_BASE_URL", "https://cms.example.org"),
        bundles=["article", "page"],
        username=os.getenv("DRUPAL_USERNAME"),
        password=os.getenv("DRUPAL_PASSWORD"),
        limit=25,
    )

    # 2. Add to Cognee memory
    await cognee.add(source)

    # 3. Cognify into knowledge graph
    await cognee.cognify()

    # 4. Search company knowledge
    results = await cognee.search("What is our remote work travel policy?")
    for res in results:
        print("-", res)

if __name__ == "__main__":
    asyncio.run(main())
```

---

## Running Tests

Run the hermetic offline test suite:

```bash
pytest packages/connector/drupal/tests
```
