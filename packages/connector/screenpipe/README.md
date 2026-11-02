# cognee-community-connector-screenpipe

A [Screenpipe](https://github.com/screenpipe/screenpipe) data-source connector for [cognee](https://github.com/topoteretes/cognee): ingest local meeting audio transcripts, captured structured memories, and windowed screen context into cognee's knowledge graph and vector memory.

## Features

- **Finished Meetings & Transcripts (`GET /meetings`, `GET /meetings/{id}/transcript`):** Ingests meetings once concluded (`meeting_end` is populated), extracting attendees, application context, notes, and speaker-labeled conversation chunks. Primary key format: `meeting:{id}`.
- **Structured Memories (`GET /memories`):** Ingests user memories with tag references and timestamps. Primary key format: `memory:{id}`.
- **Opt-in Windowed Screen OCR (`GET /search` with `content_type=ocr`):** Cleanly aggregates captured OCR lines into deterministic hourly documents per application (`screen:{device}:{app}:{hour}`), strictly gated by an explicit `app_allowlist` and automatic PII filtering.
- **Zero-Config Localhost Defaults:** Connects to `http://localhost:3030` out of the box with zero required tokens. Supports `api_key` (Bearer authentication) and custom `base_url` for remote or containerized setups.
- **Document-Mode Routing:** Decorates resources with `DOCUMENT_SOURCE_ATTR = "screenpipe"` so data passes directly into cognee's text-document cognify pipeline.
- **Deletions & Reconciliations:** Emits `_deleted` boolean markers with `write_disposition="merge"` for seamless forget-on-delete across cognee's orphan cleanup.

## Installation

```bash
uv pip install cognee-community-connector-screenpipe
# or, from this monorepo:
cd packages/connector/screenpipe && uv sync --all-extras
```

## Quick Start

```python
import asyncio
import cognee
from cognee_community_connector_screenpipe import screenpipe_source

async def main():
    # Ingest meetings and memories from local Screenpipe instance
    await cognee.remember(
        screenpipe_source(
            base_url="http://localhost:3030",
            include_meetings=True,
            include_memories=True,
        ),
        dataset_name="local_screenpipe_context",
        write_disposition="merge",
        max_rows_per_table=0,
    )

    # Query your meeting context
    answer = await cognee.search(
        query_text="What did we decide during yesterday's sync meeting?",
        query_type=cognee.SearchType.GRAPH_COMPLETION,
        datasets=["local_screenpipe_context"],
    )
    print(answer)

if __name__ == "__main__":
    asyncio.run(main())
```

## Multi-Device & Screen OCR Ingestion

```python
await cognee.remember(
    screenpipe_source(
        base_url="http://localhost:3030",
        device_id="macbook-pro-work",
        include_screen_text=True,
        app_allowlist=["Slack", "Obsidian", "Notion", "VS Code"],
    ),
    dataset_name="workstation_context",
    write_disposition="merge",
    max_rows_per_table=0,
)
```

## Running Tests

```bash
pytest packages/connector/screenpipe/tests/
```
