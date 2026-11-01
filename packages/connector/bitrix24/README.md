# Bitrix24 CRM Community Connector for Cognee

An enterprise inbound webhook connector that integrates **Bitrix24 CRM** with **Cognee**. Ingest deals, pipelines, customer contacts, corporate profiles, and chronological timeline comments directly into Cognee's cognitive memory graph.

## Features

- **Deals & Pipelines**: Ingest deal titles, stages, revenue opportunities, currencies, and timestamps.
- **Contacts & Companies**: Ingest lead contacts with phone/email channels and organizational profiles.
- **Timeline Comments**: Recursively attaches historical notes and team comments to each record.
- **Cognee-Native Documents**: Formats records as structured markdown documents ready for vector search and entity relation extraction.
- **Robust Error Handling**: Paces requests and handles 401 invalid credentials, 429 operation time limits, and 503 leaky bucket rate limits.

## Installation

```bash
pip install cognee-community-connector-bitrix24
```

## Quickstart

```python
import asyncio
from cognee_community_connector_bitrix24 import Bitrix24Connector

async def main():
    # Bitrix24 inbound webhook endpoint
    webhook_url = "https://your-domain.bitrix24.com/rest/1/your_webhook_key/"

    connector = Bitrix24Connector(webhook_url=webhook_url)

    # Ingest CRM data as Cognee documents
    docs = await connector.to_documents(
        crm_types=["deal", "contact", "company"],
        limit_per_type=25,
        include_comments=True
    )

    # Add directly to Cognee cognitive memory
    # await cognee.add(docs)
    # await cognee.cognify()

    await connector.close()

if __name__ == "__main__":
    asyncio.run(main())
```

## Running Tests

Execute the hermetic test suite offline:

```bash
poetry run pytest tests/
```
