# cognee-community-connector-discord

A Discord data-source connector for [cognee](https://github.com/topoteretes/cognee): turn
your server's conversations into memory, so you can ask "what did we decide about the
release in #general last week?".

It exposes a `dlt` source you hand to `cognee.remember(...)`. Messages are grouped into
one document per channel or thread per UTC day, and each document flows through cognee's
normal cognify entity extraction. It uses the REST API only, so no bot process has to stay
online.

## Install

```bash
uv pip install cognee-community-connector-discord
# or, from this monorepo:
cd packages/connector/discord && uv sync
```

## Setup

1. Create an application in the [Discord Developer Portal](https://discord.com/developers/applications)
   and add a bot. Copy the bot token.
2. On the **Bot** page, turn on **Message Content Intent** under Privileged Gateway Intents.
   Without it Discord returns empty messages, so the connector refuses to sync.
3. Invite the bot with **View Channels** and **Read Message History** (permissions `66560`):
   `https://discord.com/oauth2/authorize?client_id=<application id>&scope=bot&permissions=66560`.
   Give it a role that can see only the channels you want in memory.
4. Copy the server id (Developer Mode on, then right-click the server > Copy Server ID) and
   export both, together with your `LLM_API_KEY` like any other cognee run:

```bash
export DISCORD_BOT_TOKEN="..."
export DISCORD_GUILD_ID="..."
```

## Usage

```python
import cognee
from cognee_community_connector_discord import discord_source

await cognee.remember(
    discord_source(),  # DISCORD_GUILD_ID and DISCORD_BOT_TOKEN from env
    dataset_name="discord",
    primary_key="id",
    write_disposition="merge",  # REQUIRED, the add pipeline defaults to "replace"
    max_rows_per_table=0,
    self_improvement=False,
)

answer = await cognee.recall("What did we decide about the release?", datasets=["discord"])
```

Run the same call again to sync only what changed. See `examples/example.py`.

### Choosing what to ingest

| Argument | Default | Meaning |
| --- | --- | --- |
| `channel_ids` | all | Channel ids to sync. By default every text, announcement, forum and media channel. |
| `include_threads` | `True` | Active and archived public threads of text channels. |
| `include_forums` | `True` | Forum and media posts (each post is a thread). |
| `include_private_threads` | `False` | Private threads. Archived ones need **Manage Threads**. |
| `include_bot_messages` | `False` | Messages from bots and webhooks. |
| `since_days` | `90` | How far back the first sync of a channel reads. |
| `rescan_days` | `7` | Recent days re-read on every run to catch edits and deletions. |
| `full_resync` | `False` | Re-read the whole `since_days` history once. |
| `resource_name` | `discord_messages` | Staging table. Use a different name per sync scope that shares a dataset. |

Channels the bot cannot see are skipped, but each one costs a refused request per sync,
so pass `channel_ids` on servers with many private channels.

## What a document looks like

```
Channel: #release-planning
Server: Acme
Date: 2026-10-02

[10:02] Priya Shah: We need the export fix before Friday.
[10:04] Sam Lee (reply to Priya Shah): I'll take it, PR is almost ready. [attachment: plan.pdf]
```

Mentions are resolved to names, custom emoji to `:name:`, and attachments are kept as
file names only, since Discord's attachment links are signed and expire. Forum posts start
with the post title and its tags. System messages (joins, pins, boosts) are skipped.

## How sync and forget-on-delete work

- **Cursor.** Each channel and thread keeps the highest message id it has seen. Message
  ids are snowflakes that encode time, so all paging uses `after=<id>`, never timestamps.
- **Re-scan window.** A forward cursor alone never sees a message edited or deleted
  behind it, and Discord has no "changed since" endpoint (edits and deletes only arrive
  live over the Gateway). So each run re-reads from the start of the UTC day of
  `min(cursor, now - rescan_days)`. Edits and deletions in that window update their day,
  and a new message only re-processes today's document. Older days are left untouched
  and cost nothing. Use `full_resync=True` when older edits and deletions matter.
- **Forget-on-delete.** A day whose messages were all deleted, a deleted thread or forum
  post, a deleted channel, a channel the bot can no longer see, and a channel you remove
  from `channel_ids` are all removed from memory on the next sync.
- **Safe failures.** Only definitive answers (unknown channel, missing access) delete
  anything. If a channel stops returning history (Read Message History removed), it is
  left unchanged instead of looking deleted. Any other failure aborts the sync, and dlt
  keeps the previous state.
- **Rate limits.** The client waits out exhausted rate-limit buckets and honors
  `retry_after` on 429, and it never retries a 401 or 403. Discord temporarily bans an IP
  after 10,000 of those responses in 10 minutes.

## Privacy

This reads every message the bot can see. Nothing is fetched until you call `remember`,
and anyone with read access to the target dataset can read what was ingested, so keep
Discord in its own dataset and limit the bot's role. The token is kept in memory only and
never written into documents, state or error messages.

## Testing

```bash
uv run pytest tests/
```

The tests need no bot or server. They run the client against a mocked HTTP transport
(headers, rate limits, error codes), check snowflakes and message rendering, drive the
sync against a fake Discord API through a real dlt pipeline (incremental cursor, re-scan
window, deletions, failed runs), and run `cognee.remember` end to end to prove a deleted
thread leaves the graph.
