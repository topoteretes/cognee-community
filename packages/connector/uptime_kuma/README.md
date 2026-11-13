# cognee-community-connector-uptime-kuma

An Uptime Kuma data-source connector for [cognee](https://github.com/topoteretes/cognee):
sync infrastructure monitors, heartbeats, downtime incidents, and status-page announcements into AI agent memory.

It exposes a `dlt` source passed directly to `cognee.remember(...)` / `cognee.add(...)`. Monitored services, incident summaries, and latency health are rendered into high-signal Markdown documents and ingested through Cognee's `cognify()` pipeline (via document-mode `DOCUMENT_SOURCE_ATTR`), building an interconnected knowledge graph of your servers, endpoints, error codes, and maintenance states.

## Features

- **Service Monitors & Endpoints**: Ingests HTTP, Ping, Port, DNS, Keyword, and Docker monitor definitions (target URL, interval, current UP/DOWN status).
- **Incident & Heartbeat History**: Captures historical outage events, error descriptions (e.g. `502 Bad Gateway`, `Connection Refused`), and response latency (ms).
- **Public & Private Status Pages**: Syncs status-page groupings, active incident announcements, and maintenance windows.
- **Full-Snapshot Sync & Forget-on-Delete**: Configured with `write_disposition="replace"` so deleted monitors or resolved maintenance periods are automatically purged from the knowledge graph via Cognee's orphan cleanup.
- **Resilient Client**: Built-in exponential retry backoff on HTTP 429 and defensive token scrubbing from logs.

## Requirements

Requires a Cognee release supporting document mode (`cognee.tasks.ingestion.dlt_utils.DOCUMENT_SOURCE_ATTR`), shipped in Cognee 1.4.0+.

## Installation

```bash
uv pip install cognee-community-connector-uptime-kuma
# or, from this monorepo:
cd packages/connector/uptime_kuma && uv sync --all-extras
```

## Quickstart

```python
import os
import asyncio
import cognee
from cognee_community_connector_uptime_kuma import uptime_kuma_source


async def main():
    # Configure Uptime Kuma instance and API token or status page slug
    source = uptime_kuma_source(
        base_url="https://status.example.com",
        api_key=os.environ.get("UPTIME_KUMA_API_KEY"),
        slug="main",
    )

    # Ingest into Cognee memory
    await cognee.remember(source, dataset_name="infrastructure_monitoring")

    # Query the knowledge graph
    answer = await cognee.search(
        query_text="Which services experienced outages recently and what were the error messages?",
        query_type=cognee.SearchType.GRAPH_COMPLETION,
        datasets=["infrastructure_monitoring"],
    )
    print(answer)


if __name__ == "__main__":
    asyncio.run(main())
```

## Configuration Options

`uptime_kuma_source(...)` accepts the following arguments:

| Argument | Type | Default | Description |
| :--- | :--- | :--- | :--- |
| `base_url` | `str` | `"http://localhost:3001"` | Base URL of your self-hosted Uptime Kuma instance. |
| `api_key` | `str` | `None` | Optional API token for authenticated endpoints. |
| `slug` | `str` | `"default"` | Status page slug to query monitor groupings and incidents. |
| `include_heartbeats` | `bool` | `True` | Whether to fetch latency metrics and recent incident events. |
| `include_incidents` | `bool` | `True` | Whether to fetch status-page incident announcements. |
| `client` | `Any` | `None` | Pre-built HTTP client (used for testing and dependency injection). |

## How Sync and Forget-on-Delete Work

The connector operates as a **full snapshot** (`write_disposition="replace"`):
1. Each run collects current active monitors, recent incident histories, and maintenance posts.
2. Unchanged documents maintain stable deterministic content IDs (`uptime_kuma:<slug_or_id>:<type>`), avoiding duplicate extraction or unnecessary LLM calls.
3. If a monitor is removed or an incident resolved and archived, Cognee's built-in `orphan_cleanup` purges the obsolete entities and vectors from graph and vector databases.

## Testing

Run the offline test suite:

```bash
uv run pytest tests/
```

All unit tests run completely offline using mocked HTTP fixtures (covering authentication, monitor parsing, heartbeat incident formatting, and error handling).
