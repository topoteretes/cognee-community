# lemlist Sales Outreach Community Connector for Cognee

An enterprise REST API connector integrating **lemlist** with **Cognee**. Ingest cold outreach campaigns, sequence statuses, send volumes, open rates, click percentages, and reply conversions into Cognee's cognitive knowledge graph.

## Features

- **Campaign Metagraphics**: Ingests campaign names, statuses (active, paused, draft, ended), schedules, timezones, and thematic tags.
- **Aggregated Performance Metrics**: Computes and normalizes email dispatch volumes, unique opens, link clicks, direct replies, and bounces.
- **Prospect Privacy by Design**: Protects individual lead emails and personal reply contents by ingesting high-level campaign metrics rather than confidential recipient communications.
- **Robust Authentication & Error Handling**: Seamless HTTP Basic Auth using lemlist API keys with dedicated error parsing for 401 unauthorized and 429 rate limits.
- **Hermetic Pytest Suite**: 100% offline mock test suite covering campaign manifests, embedded statistics, standalone stat endpoints, rate calculations, and error resilience.

## Installation

```bash
pip install cognee-community-connector-lemlist
```

## Quickstart

```python
import asyncio
from cognee_community_connector_lemlist import LemlistConnector

async def main():
    api_key = "your_lemlist_api_key"

    connector = LemlistConnector(api_key=api_key)

    # Ingest campaigns and metrics into Cognee
    documents = await connector.to_documents(limit=25, include_stats=True)

    # await cognee.add(documents)
    # await cognee.cognify()

    await connector.close()

if __name__ == "__main__":
    asyncio.run(main())
```

## Running Tests

Execute the hermetic unit tests offline:

```bash
pytest packages/connector/lemlist/tests/test_lemlist.py -v
```
