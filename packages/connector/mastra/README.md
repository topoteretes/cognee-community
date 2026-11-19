# Cognee Community Connector: Mastra.ai

Data-source connector syncing [Mastra.ai](https://mastra.ai) agent conversation threads, messages, and tool interactions into Cognee's cognitive memory graph.

## Overview

Mastra applications generate rich knowledge through conversational agent threads, user messages, and tool execution outputs. This connector:
- Ingests persisted conversation threads and chronologically orders all dialogue turns.
- Renders speaker roles (`user`, `assistant`), messages, and tool invocations into readable Markdown.
- Sets `DOCUMENT_SOURCE_ATTR = "mastra"` to route conversations through Cognee's `cognify()` entity extraction pipeline.
- Uses full-snapshot syncing (`write_disposition="replace"`) for automatic orphan cleanup.

## Installation

```bash
pip install cognee-community-connector-mastra
```

## Configuration

Set the environment variables or pass them directly to `mastra_source()`:
- `MASTRA_API_URL`: URL of the Mastra deployment (default: `http://localhost:4111`).
- `MASTRA_API_KEY`: Optional Bearer token or API key for protected deployments.

## Usage

```python
import asyncio
import os
import cognee
from cognee_community_connector_mastra import mastra_source

async def main():
    source = mastra_source(
        api_url=os.environ.get("MASTRA_API_URL", "http://localhost:4111"),
        api_key=os.environ.get("MASTRA_API_KEY"),
        include_tools=True,
    )

    await cognee.add(source)
    await cognee.cognify()

    results = await cognee.search("What solutions were discussed for billing issues?")
    print(results)

if __name__ == "__main__":
    asyncio.run(main())
```

## Running Tests

Tests execute hermetically without requiring an active Mastra deployment:
```bash
pytest packages/connector/mastra/tests/
```
