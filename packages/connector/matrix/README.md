# cognee-community-connector-matrix

A [Matrix](https://matrix.org) / Element data-source connector for
[cognee](https://github.com/topoteretes/cognee): sync room messages and thread replies
into memory — "ask my team chat". Works with any homeserver (matrix.org, self-hosted
Synapse / Dendrite / Conduit, Element-hosted).

It exposes a `dlt` resource you hand to `cognee.remember(...)`, reusing cognee's DLT
ingestion path, so you get **incremental sync** (`/sync` `since` token) and
**forget-on-delete** (redacted messages and rooms you leave are purged on the next sync).

## Install

```bash
cd packages/connector/matrix && uv sync --all-extras
```

## Usage

```python
import cognee
from cognee_community_connector_matrix import matrix_source

await cognee.remember(
    matrix_source(
        homeserver="https://matrix.org",
        access_token="syt_...",
        room_ids=["!abc123:matrix.org"],  # None = every joined room
    ),
    dataset_name="team_chat",
    primary_key="id",
    write_disposition="merge",  # required: the default "replace" wipes on re-sync
    max_rows_per_table=0,
)
```

Re-run `remember(...)` with the same dataset to sync only what changed. See
`examples/example.py`.

## What is ingested

One row per text message (`m.text`, `m.notice`, `m.emote`), keyed by `event_id`, with
`room_id`, `room_name`, `sender`, `sent_at`, `thread_root`, `reply_to`, a `matrix.to`
permalink and the text prefixed with who/where/when.

| Upstream change | Effect on memory |
| --- | --- |
| New message / thread reply | Added |
| Message edited (`m.replace`) | Original row updated with the new text |
| Message redacted | Removed (`_deleted` hard-delete marker → `orphan_cleanup`) |
| Account leaves the room | Every message from that room removed (`forget_on_leave=True`, default) |

Set `forget_on_leave=False` if leaving a room should only stop future sync for
that room and keep previously ingested messages (leave is an access change, not
an upstream deletion).

## Auth / permissions

- Homeserver Client-Server API base URL (`MATRIX_HOMESERVER`)
- Access token of a user or bot that has already joined the target rooms
  (`MATRIX_ACCESS_TOKEN`) — Element → Settings → Help & About → Access token
- Optional room allowlist (`MATRIX_ROOM_IDS`, comma-separated `!room:server` ids)
- The connector issues **GET-only** requests (`/sync`, `/rooms/.../messages`)

## Limitations

- **End-to-end encrypted rooms are skipped** (`m.room.encrypted` cannot be read without
  device keys). Use unencrypted rooms or invite a dedicated bot account.
- Files, images and other non-text messages are skipped.
- The first sync backfills at most `max_backfill_events` (default 1000) per room.
- Room history visibility may hide older events from the syncing account; the
  connector only ingests what the token can see and does not invent deletions
  from 401/403 responses.

## Tests

```bash
uv run pytest tests        # fully mocked, no homeserver needed
```
