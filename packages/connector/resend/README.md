# Cognee Community Connector: Resend

A data-source connector for [Cognee](https://github.com/topoteretes/cognee) that synchronizes domains, sent emails, contact audiences, and API key metadata from [Resend](https://resend.com/) into Cognee persistent memory and knowledge graphs.

## Features

- **Resource Coverage:** Synchronizes Resend verified domains (`/domains`), sent emails (`/emails`), contact audiences (`/audiences`), and API key metadata (`/api-keys`).
- **Incremental State Sync:** Deterministic SHA-256 fingerprinting on canonical rows ensures only modified or new entities mutate the graph.
- **Forget-on-Delete:** Emits `{"_deleted": True}` tombstones to cleanly prune deleted domains, audiences, or keys from Cognee memory.
- **Strict Secret & PII Redaction:** Plaintext API keys and recipient email addresses are strictly masked (`[Configured & Protected]`, `jo***@domain.com`).
- **Resilient HTTP Client:** Implements exponential backoff retry on HTTP 429 rate limits and 5xx server errors.
- **100% Offline Testing:** Tested with comprehensive in-memory mock clients without requiring live API keys or network access.

## Installation

```bash
uv pip install -e packages/connector/resend
```

## Quickstart

```python
import asyncio
import os
import cognee
from cognee_community_connector_resend import resend_source


async def main():
    api_key = os.environ.get("RESEND_API_KEY", "re_mock_key")
    pipeline = resend_source(api_key=api_key, include=("domains", "audiences"))
    await cognee.add(pipeline)
    await cognee.cognify()

    results = await cognee.recall("Which domains are verified in Resend?")
    for item in results:
        print(item)


if __name__ == "__main__":
    asyncio.run(main())
```
