# cognee-community-connector-google-chat

A Google Chat data source for [Cognee](https://github.com/topoteretes/cognee). It syncs selected
named spaces as one Cognee document per thread, preserving message order and sender context.

## Install

```bash
uv pip install cognee-community-connector-google-chat
# or, from this repository:
cd packages/connector/google-chat && uv sync --all-extras
```

## Google setup

1. Use a Google Workspace account with access to the spaces you want to sync. Google Chat API
   access for this connector requires a supported Workspace account.
2. In a Google Cloud project, enable the Google Chat API and create an OAuth client ID of type
   **Desktop app**. Download its client JSON file. The connector uses installed-app user OAuth
   with the read-only `chat.spaces.readonly` and `chat.messages.readonly` scopes.
3. Make sure the authenticating user can view each configured space. The first run opens a browser
   consent flow and writes a refreshable token file with owner-only permissions.

The connector does not enumerate all spaces. Supply resource names explicitly as
`spaces/{space_id}`. Only named spaces (`spaceType == "SPACE"`) are supported; direct messages
and group chats are rejected.

## Usage

```python
import asyncio

import cognee
from cognee_community_connector_google_chat import google_chat_source


async def main():
    source = google_chat_source(
        ["spaces/AAAAExample"],
        credentials_path="credentials.json",
        token_path="token.json",
    )
    await cognee.remember(
        source,
        dataset_name="team-google-chat",
        max_rows_per_table=0,
    )


if __name__ == "__main__":
    asyncio.run(main())
```

See [`examples/example.py`](examples/example.py). Keep a dedicated dataset for this Google Chat
source so Cognee's source cleanup stays scoped to the intended data.

## Sync behavior

- The first run lists messages in each selected space and groups them into thread documents.
- Later runs consume Google Chat space events and rebuild affected threads from their current
  messages. A periodic full reconcile covers the API's 28-day event retention window.
- Deleted threads and spaces removed from the selection emit dlt hard-delete markers.
- Sync state tracks a cursor and message-to-thread mapping per space. Preserve the same dlt
  pipeline/source state between runs to get incremental sync and deletion handling.
- Each complete sync preflights the selected spaces before reading messages. API errors abort the
  sync rather than advancing the cursor or treating unread data as deleted.

## Tests

```bash
uv run pytest tests/
```

Tests use fake Google API services and a local SQLite dlt destination; they do not require Google
credentials or network access.
