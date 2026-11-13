# Medium Connector for Cognee

A `dlt` data-source connector that ingests public Medium articles, tutorials, and publication feeds into Cognee memory.

Part of the **Cognee Mergetober Hackathon** (`#5718`).

---

## Features

- **Article & Feed Ingestion:** Ingest articles via direct URLs or author (`@username`) and publication feeds.
- **Structured Content Extraction:** Extracts article titles, authors, publication dates, tags, and converts HTML body into clean markdown.
- **Full Snapshot & Forget-on-Delete:** Uses `write_disposition="replace"` so removed articles automatically clear from Cognee's graph during orphan cleanup.
- **Duplicate Prevention:** Normalizes URLs to canonical form and hashes identifiers to avoid duplicate ingestion.
- **Document Mode:** Tags resources with `DOCUMENT_SOURCE_ATTR` to route directly through Cognee's `cognify` entity-extraction pipeline.

---

## Quickstart

```python
import asyncio
import cognee
from cognee_community_connector_medium import medium_source

async def main():
    # 1. Initialize Medium source
    source = medium_source(
        authors=["@towardsdatascience"],
        article_urls=[
            "https://medium.com/@author/building-ai-agents-with-memory-12345",
        ],
    )

    # 2. Add to Cognee memory
    await cognee.add(source)

    # 3. Cognify into knowledge graph
    await cognee.cognify()

    # 4. Search over ingested Medium knowledge
    results = await cognee.search("What are best practices for memory-enabled AI agents?")
    for res in results:
        print("-", res)

if __name__ == "__main__":
    asyncio.run(main())
```

---

## Running Tests

Run the hermetic test suite:

```bash
pytest packages/connector/medium/tests
```
