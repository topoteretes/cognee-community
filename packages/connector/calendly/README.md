# Calendly connector for cognee

Sync Calendly scheduled events and invitee context into cognee.

## What is ingested

Each Calendly invitee becomes a searchable record containing:

- event name and scheduled time
- invitee name and email
- invitee question-and-answer responses
- event metadata and notes

Invitee responses are included because they often contain the meaningful context for a scheduled meeting.

## Requirements

- Python 3.11-3.13
- A Calendly Personal Access Token

## Setup

Create a Calendly Personal Access Token and export it:

    export CALENDLY_API_TOKEN="your-token"

Install dependencies from this package directory:

    uv sync

## Usage

    import asyncio

    import cognee

    from cognee_community_connector_calendly import calendly_source


    async def main():
        await cognee.remember(
            calendly_source(),
            dataset_name="calendly",
        )


    asyncio.run(main())

## Incremental sync

The connector persists an incremental cursor and uses Calendly's `min_start_time`
window on subsequent runs so later syncs request only events in the selected
incremental window.

## Deletions

Cancelled or deleted upstream events are emitted through the connector's
deletion path so cognee can remove the corresponding record during the next
synchronization.

## Example

See `examples/example.py` for a runnable example.

## Tests

From this package directory:

    uv run pytest tests/ -v
