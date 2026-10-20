# Cognee Community Connector: Zoho Cliq

Data-source connector syncing [Zoho Cliq](https://www.zoho.com/cliq/) team chat channels, discussions, and thread replies into Cognee's cognitive memory graph.

## Overview

A large portion of team engineering decisions, incident root causes, and project updates happen inside team chat channels. This connector:
- Ingests top-level messages and folds threaded replies into their parent context.
- Strips deleted messages and tombstoned updates.
- Formats messages into clean Markdown documents with channel and sender attribution.
- Declares `DOCUMENT_SOURCE_ATTR = "zoho_cliq"` to route discussions through Cognee's `cognify()` entity extraction pipeline.
- Uses full-snapshot syncing (`write_disposition="replace"`) for seamless orphan cleanup.

## Installation

```bash
pip install cognee-community-connector-zoho-cliq
```

## Configuration

Set the environment variables or pass them directly to `zoho_cliq_source()`:
- `ZOHO_CLIQ_AUTH_TOKEN`: Zoho OAuth access token with read scopes (`ZohoCliq.Channels.READ`, `ZohoCliq.Chats.READ`, `ZohoCliq.Messages.READ`).
- `ZOHO_CLIQ_DOMAIN`: Regional domain (e.g., `cliq.zoho.com`, `cliq.zoho.in`, `cliq.zoho.eu`). Default: `cliq.zoho.com`.

## Usage

```python
import asyncio
import os
import cognee
from cognee_community_connector_zoho_cliq import zoho_cliq_source

async def main():
    source = zoho_cliq_source(
        auth_token=os.environ["ZOHO_CLIQ_AUTH_TOKEN"],
        domain="cliq.zoho.com",
        channel_names=["general", "engineering-sync"],
        include_threads=True,
    )

    await cognee.add(source)
    await cognee.cognify()

    results = await cognee.search("What was decided about the release schedule?")
    print(results)

if __name__ == "__main__":
    asyncio.run(main())
```

## Running Tests

Tests run in hermetic mode without live credentials:
```bash
pytest packages/connector/zoho-cliq/tests/
```
