# Cognee Community Connector: Meilisearch

Community data-source connector for [Meilisearch](https://www.meilisearch.com/) lightning-fast search engine and document indexes into [Cognee](https://github.com/topoteretes/cognee).

## Overview
Meilisearch is an ultra-relevant, open-source search engine used by thousands of engineering teams. Integrating Meilisearch index documents directly into Cognee enables AI agents to query structured search indexes, e-commerce product catalogs, and document archives in unified Graph RAG memory.

## Features
- **Auto Index Discovery:** Dynamically discovers all accessible indexes or ingests explicitly configured index UIDs.
- **Deterministic Document Extraction:** Extracts paginated index documents with configurable field allowlists.
- **Cognitive Document Formatting:** Transforms key-value documents into structured markdown ready for LLM vectorization.
- **Flexible Authentication:** Supports both Master/API Key Bearer authentication and local development setups.
- **100% Hermetic Testing:** Tested offline with zero external network or local daemon requirements.

## Installation
```bash
pip install cognee-community-connector-meilisearch
```

## Quickstart
```python
import asyncio
from cognee_community_connector_meilisearch import MeilisearchConnector
import cognee

async def main():
    connector = MeilisearchConnector(
        host="http://localhost:7700",
        api_key="your_meilisearch_key",
        indexes=["movies", "catalog"],
        limit_per_index=50,
    )

    documents = await connector.fetch_all()
    print(f"Extracted {len(documents)} documents from Meilisearch.")

    for doc in documents:
        await cognee.add(doc["clean_text"])

    await cognee.cognify()
    print("Ingestion complete!")

if __name__ == "__main__":
    asyncio.run(main())
```
