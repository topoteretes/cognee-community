# Cognee Community Connector: Knock

A data-source connector for [Cognee](https://github.com/topoteretes/cognee) that synchronizes notification workflows, delivery channels, schedules, and message statuses from [Knock](https://knock.app/) into Cognee persistent memory and knowledge graphs.

## Features

- **Resource Coverage:** Synchronizes Knock workflows (`/v1/workflows`), delivery channels (`/v1/channels`), notification schedules (`/v1/schedules`), and message statuses (`/v1/messages`).
- **Incremental State Sync:** Deterministic SHA-256 fingerprinting on canonical rows ensures only modified or new entities mutate the graph.
- **Forget-on-Delete:** Emits `{"_deleted": True}` tombstones to cleanly prune deleted workflows, channels, or schedules from Cognee memory.
- **Strict Secret & PII Redaction:** Delivery provider credentials and recipient contact information are masked (`[Configured & Protected]`, `[Redacted]`).
- **Resilient HTTP Client:** Implements cursor-based pagination and exponential backoff retry on HTTP 429 rate limits and 5xx errors.
- **100% Offline Testing:** Tested with comprehensive in-memory mock clients without requiring live API keys or network access.

## Installation

```bash
uv pip install -e packages/connector/knock
```

## Quickstart

```python
import asyncio
import os
import cognee
from cognee_community_connector_knock import knock_source


async def main():
    api_key = os.environ.get("KNOCK_API_KEY", "mock_key")
    pipeline = knock_source(api_key=api_key, include=("workflows", "channels"))
    await cognee.add(pipeline)
    await cognee.cognify()

    results = await cognee.recall("What delivery channels are configured in Knock?")
    for item in results:
        print(item)


if __name__ == "__main__":
    asyncio.run(main())
```
