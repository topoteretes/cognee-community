# cognee-community-connector-calendly

A Calendly data-source connector for [cognee](https://github.com/topoteretes/cognee):
sync scheduled events, meeting invitees, questions & answers, and notes into memory.

It exposes a `dlt` source you pass to `cognee.remember(...)` / `cognee.add(...)`. Events
are rendered into structured markdown documents and ingested as **normal documents**
(flowing through cognee's LLM cognify entity-extraction pipeline, chunking, and knowledge graph).

## Requirements

> **This connector requires a cognee release that ships "document-mode"** — i.e.
> `cognee.tasks.ingestion.dlt_utils.DOCUMENT_SOURCE_ATTR` and the `resolve_dlt_sources`
> routing that reads it (shipped in cognee 1.4.0+).

## Install

```bash
uv pip install cognee-community-connector-calendly
# or, from this monorepo:
cd packages/connector/calendly && uv sync
```

## Quick Start

```python
import asyncio
import cognee
from cognee_community_connector_calendly import calendly_source


async def main():
    # Ingest active scheduled events and invitee responses into memory
    await cognee.remember(
        calendly_source(),  # reads CALENDLY_API_KEY from environment
        dataset_name="calendly",
    )

    # Search across meeting notes and attendee responses
    results = await cognee.search(
        query_text="What questions did clients ask before our strategy sessions?",
        query_type=cognee.SearchType.GRAPH_COMPLETION,
        datasets=["calendly"],
    )
    print(results)


if __name__ == "__main__":
    asyncio.run(main())
```

## How It Works

### 1. Ingesting Events & Invitee Context
The connector retrieves scheduled events via `GET /scheduled_events` and automatically
fetches attendees and their booking question responses via `GET /scheduled_events/{uuid}/invitees`.
Invitee questions carry critical business context that standard calendar slots omit.

### 2. Incremental Sync
Use `min_start_time` and `max_start_time` (ISO 8601 strings) to filter the sync window:

```python
source = calendly_source(min_start_time="2026-10-01T00:00:00Z")
```

Unchanged events produce a stable content hash ID, avoiding duplicate processing or re-cognition.

### 3. Forget-on-Delete & Cancellation Handling
The source operates in **full-snapshot replace mode** (`write_disposition="replace"`):
- Canceled events (`status != "active"`) or deleted meetings vanish from the snapshot.
- Cognee's `orphan_cleanup` purges missing items from the knowledge graph and vector database.

## Setup & Scopes

1. Generate a Personal Access Token from [Calendly Integrations](https://calendly.com/integrations).
2. Ensure the token has the `scheduled_events:read` scope.
3. Export your token:
   ```bash
   export CALENDLY_API_KEY="your_personal_access_token"
   ```

## Testing

```bash
uv run --directory packages/connector/calendly --with pytest pytest tests/
```
