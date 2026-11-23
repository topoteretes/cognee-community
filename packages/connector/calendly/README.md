# cognee-community-connector-calendly

A Calendly data-source connector for [cognee](https://github.com/topoteretes/cognee):
sync scheduled events, invitee questions, and notes into memory.

Addresses good-first-issue #4816.

## Install

```bash
uv pip install cognee-community-connector-calendly
```

## Usage

```python
import cognee
from cognee_community_connector_calendly import calendly_source

await cognee.remember(
    calendly_source(),  # CALENDLY_API_TOKEN from env
    dataset_name="calendly",
)
```

## Setup

1. Create a personal access token in your Calendly account (Integrations > API & Webhooks).
2. Set `CALENDLY_API_TOKEN`.

This is a starter implementation. Full incremental sync (`min_start_time`), invitee question extraction, and forget-on-delete should follow the Notion reference and Calendly's scheduled_events API.
