# cognee-community-connector-opsgenie

Atlassian Opsgenie incident and on-call data-source connector for [cognee](https://github.com/topoteretes/cognee).

Syncs enterprise alerts, incident severities (P1–P5), responder escalations, and on-call schedule rotations into Cognee memory and knowledge graphs.

## Features

- **Alert Telemetry**: Ingests active and historical alerts, priority levels (P1–P5), responder assignments, tags, and diagnostics.
- **On-Call Visibility**: Ingests schedules, rotations, and active on-call engineers.
- **Escalation Routing**: Ingests multi-tier escalation policies, delay intervals, and fallback responders.
- **Forget-On-Delete**: Uses `write_disposition="replace"` so resolved or deleted alerts automatically drop out of Cognee's graph via orphan cleanup.
- **Query Scoping**: Filter alerts using native Opsgenie query syntax (e.g. `status: open AND priority: P1`).

## Installation

```bash
pip install cognee-community-connector-opsgenie
```

## Quickstart

```python
import asyncio
import cognee
from cognee_community_connector_opsgenie import opsgenie_source


async def main():
    source = opsgenie_source(
        api_key="your-opsgenie-genie-key",
        query="status: open",
    )

    await cognee.add(source)
    await cognee.cognify()

    results = await cognee.search("Who is currently on-call for critical P1 alerts?")
    print(results)


if __name__ == "__main__":
    asyncio.run(main())
```

## Environment Variables

- `OPSGENIE_API_KEY`: Opsgenie API GenieKey token.
- `OPSGENIE_BASE_URL`: Base API URL (e.g. `https://api.opsgenie.com` or `https://api.eu.opsgenie.com`).
