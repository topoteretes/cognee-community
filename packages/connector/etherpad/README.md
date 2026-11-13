# Cognee Community Connector: Etherpad

Data-source connector syncing [Etherpad](https://github.com/ether/etherpad-lite) collaborative pads and chat history into Cognee's cognitive memory graph.

## Overview

Etherpad is widely used by open-source communities, education institutions, and teams for collaborative meeting agendas, architectural decisions, and working notes. This connector:
- Ingests pad text and formats it into clean Markdown documents.
- Resolves read-only URLs (`getReadOnlyID`) so links in memory never leak edit access.
- Optionally folds in side chat discussions (`include_chat=True`).
- Sets `DOCUMENT_SOURCE_ATTR = "etherpad"` to route documents through Cognee's `cognify()` LLM entity extraction pipeline.
- Implements full-snapshot syncing (`write_disposition="replace"`) to support forget-on-delete orphan cleanup.

## Installation

```bash
pip install cognee-community-connector-etherpad
```

## Prerequisites

Run Etherpad locally via Docker:
```bash
docker run -d -p 9001:9001 --name etherpad etherpad/etherpad:latest
```
Retrieve the generated API key:
```bash
docker exec etherpad cat APIKEY.txt
```

## Configuration

Set the environment variables or pass them directly to `etherpad_source()`:
- `ETHERPAD_BASE_URL`: URL of the Etherpad instance (default: `http://localhost:9001`).
- `ETHERPAD_API_KEY`: Administrative API key (from `APIKEY.txt`).
- `ETHERPAD_BEARER_TOKEN`: Alternative OAuth2/OIDC bearer token if running in SSO mode.

## Usage

```python
import asyncio
import os
import cognee
from cognee_community_connector_etherpad import etherpad_source

async def main():
    source = etherpad_source(
        base_url="http://localhost:9001",
        api_key=os.environ["ETHERPAD_API_KEY"],
        pad_prefix="meeting-",
        include_chat=True,
    )

    await cognee.add(source)
    await cognee.cognify()

    results = await cognee.search("What did the team decide in the meeting?")
    print(results)

if __name__ == "__main__":
    asyncio.run(main())
```

## Running Tests

Tests run in hermetic mode without requiring a running Etherpad instance:
```bash
pytest packages/connector/etherpad/tests/
```
