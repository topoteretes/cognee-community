# Better Stack (Better Uptime) Connector for Cognee

A community data-source connector that syncs uptime monitor configurations, outage reports, and chronological incident timelines from [Better Stack](https://betterstack.com/uptime) into Cognee's AI agent memory.

## Features

- **Outage Context:** Ingests incident names, causes, timestamps, and error responses.
- **Chronological Milestones:** Ingests timeline events from alert trigger to acknowledgment and resolution.
- **Document-Mode Cognification:** Integrates with Cognee's prose document path (`cognee_document_source = "better_stack"`), allowing natural language queries on uptime and infrastructure health.
- **Full Snapshot Sync:** Deletions or resolved false alarms are pruned via Cognee's orphan cleanup.

## Setup & Configuration

### 1. Prerequisites

Obtain your API token from Better Stack (`Integrations` -> `API tokens`).

Set the API token in your environment:
```bash
export BETTER_STACK_API_TOKEN="your-better-stack-api-token"
```

### 2. Installation

```bash
pip install -e packages/connector/better_stack
```

## Usage

```python
import asyncio
import cognee
from cognee_community_connector_better_stack import better_stack_source

async def main():
    # 1. Initialize connector
    source = better_stack_source()

    # 2. Add and Cognify into memory
    await cognee.add(source)
    await cognee.cognify()

    # 3. Query uptime memory
    results = await cognee.search("Which services experienced downtime last week?")
    for result in results:
        print(result)

if __name__ == "__main__":
    asyncio.run(main())
```

## Running Tests

Run the offline unit tests with pytest:

```bash
pytest packages/connector/better_stack/tests
```
