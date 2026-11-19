# Cognee Community Connector: PagerDuty

Sync PagerDuty incidents, services, and response notes directly into Cognee knowledge graphs and vector memory.

## Features

- **Dual-Resource Ingestion:** Ingests live incidents (`/incidents`) and service topologies (`/services`) into structured Markdown documents.
- **Incident Intelligence:** Serializes incident numbers, titles, urgency, status, escalation policies, assignees, and historical timeline notes.
- **Pre-Ingestion Secret Scrubbing:** Automatically scrubs API keys, Bearer tokens, AWS keys, and credentials from incident titles, descriptions, and notes.
- **Rate-Limit Resilience:** Automatic exponential backoff on HTTP 429 (`Retry-After`) and transient 5xx errors.
- **Cognee Document Mode:** Declares `cognee_document_source = "pagerduty"` for automatic entity extraction and graph building.

## Installation

```bash
pip install cognee-community-connector-pagerduty
```

## Setup & Authentication

Generate a REST API key in your PagerDuty account (**Integrations > API Access Keys**).
Configure your token via environment variable or constructor:

```bash
export PAGERDUTY_API_KEY="your_pagerduty_api_token"
```

## Quick Start

```python
import asyncio
import cognee
from cognee_community_connector_pagerduty import pagerduty_source


async def main():
    # Configure source
    source = pagerduty_source(
        resources=["incidents", "services"],
        urgencies=["high"],
    )

    # Ingest and extract knowledge graph
    await cognee.add(source)
    await cognee.cognify()

    # Search incident memory
    results = await cognee.search("What incidents affected the database cluster?")
    for res in results:
        print(res)


if __name__ == "__main__":
    asyncio.run(main())
```

## Running Tests

Run the full offline test suite with zero network calls:

```bash
python run_tests.py
# or
pytest tests/
```
