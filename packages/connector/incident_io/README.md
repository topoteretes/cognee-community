# incident.io Connector for Cognee

A community data-source connector that synchronizes incident response records, post-mortem timelines, and remediation action items from [incident.io](https://incident.io/) directly into Cognee's AI agent memory.

## Features

- **Incident Context:** Ingests incident names, severity ratings, lead responders, status progressions, and summaries.
- **Chronological Milestones:** Ingests live timeline updates and timestamps during outage mitigation.
- **Action Item Tracking:** Ingests remediation follow-ups, assignees, and completion checkboxes.
- **Document-Mode Cognification:** Integrates with Cognee's prose document path (`cognee_document_source = "incident_io"`), allowing agents to search past outages naturally.
- **Full Snapshot Sync:** Deletions or purged incidents are cleanly forgotten via Cognee's orphan cleanup.

## Setup & Configuration

### 1. Prerequisites

Obtain an API key from your incident.io dashboard (`Settings` -> `API Keys`).

Set the API key in your environment:
```bash
export INCIDENT_IO_API_KEY="your-incident-io-api-key"
```

### 2. Installation

```bash
pip install -e packages/connector/incident_io
```

## Usage

```python
import asyncio
import cognee
from cognee_community_connector_incident_io import incident_io_source

async def main():
    # 1. Initialize connector
    source = incident_io_source()

    # 2. Add and Cognify into memory
    await cognee.add(source)
    await cognee.cognify()

    # 3. Query operational memory
    results = await cognee.search("What caused the recent billing service timeout?")
    for result in results:
        print(result)

if __name__ == "__main__":
    asyncio.run(main())
```

## Running Tests

Run the offline unit tests with pytest:

```bash
pytest packages/connector/incident_io/tests
```
