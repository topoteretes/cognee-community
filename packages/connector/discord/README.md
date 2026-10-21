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
| `channel_ids` | all | Channel ids to sync. By default every text, announcement, forum and media channel. A listed channel that is missing or cannot be read raises. |
| `include_threads` | `True` | Active and archived public threads of text channels. |
| `include_forums` | `True` | Forum and media posts (each post is a thread). |
| `include_private_threads` | `False` | Private threads. Archived ones need **Manage Threads**; without it the channel still syncs and its known private threads are kept. |
| `include_bot_messages` | `False` | Messages from bots and webhooks. |
| `since_days` | `90` | How far back the first sync of a channel reads. |
| `rescan_days` | `7` | Recent days re-read on every run to catch edits and deletions, never further back than `since_days`. With `0` only the cursor's day and newer messages are read. |
| `full_resync` | `False` | Re-read the whole `since_days` history once. A resync the budget cuts short resumes where it stopped on the next run with the flag still set. |
| `max_requests` | `2000` | Request budget of one run, checked once a new day or a whole channel is done. A bigger server finishes over several runs. |
| `resource_name` | `discord_messages` | Staging table. Use a different name per sync scope that shares a dataset. |

Channels the bot cannot read are left as they are and counted in `unreadable`, but each
one costs a refused request per sync, so pass `channel_ids` on servers with many private
channels.

## What a document looks like

```
Channel: #release-planning
Server: Acme
Date: 2026-10-02

[10:02] Priya Shah: We need the export fix before Friday.
[10:04] Sam Lee (reply to Priya Shah): I'll take it, PR is almost ready. [attachment: plan.pdf]
```

Mentions are resolved to names, custom emoji to `:name:`, and attachments are kept as
file names only, since Discord's attachment links are signed and expire. Embeds from bots
and webhooks (author, title, description, link, fields and footer), sticker names and polls
are kept as text; link previews are left out, since they repeat a URL already in the
message. Forum posts start
with the post title and its tags. System messages (joins, pins, boosts) are skipped.

The server name is part of every document. After a rename, days inside the re-scan window
pick up the new name and older ones keep the old one until a `full_resync`.

## How sync and forget-on-delete work

- **Cursor.** Each channel and thread keeps the highest message id it has seen. Message
  ids are snowflakes that encode time, so all paging uses `after=<id>`, never timestamps.
- **Re-scan window.** A forward cursor alone never sees a message edited or deleted
  behind it, and Discord has no "changed since" endpoint (edits and deletes only arrive
  live over the Gateway). So each run re-reads from the start of the UTC day of
  `min(cursor, now - rescan_days)`, never before `since_days`, so the re-scan never
  ingests days the first sync left out. Edits and deletions in that window update their day,
  and a new message only re-processes today's document. A channel or thread with nothing
  new and nothing inside the window costs no request, and older days are left untouched.
  Use `full_resync=True` when older edits and deletions matter.
- **Known limits of the window.** Deleting every message of a day older than the window
  keeps that day's document until a `full_resync`: the channel's `last_message_id` can
  still point at the deleted message, so the channel looks unchanged and is skipped, and
  checking every quiet channel's newest message would cost a request each. Raising
  `since_days` later re-emits a day that had aged out of the old window once, since only
  its key was kept.
- **Forget-on-delete.** A day whose messages were all deleted, a deleted thread or forum
  post, a channel that is deleted (gone from the server's channel list), and a channel you
  remove from `channel_ids` are all removed from memory on the next sync.
- **Safe failures.** A channel the bot cannot read (missing access or permissions, or Read
  Message History removed, which makes Discord return an empty list) is left unchanged
  with its threads and counted in `unreadable`, instead of looking deleted. Deselect it to
  forget it. Any other failure aborts the sync, and dlt keeps the previous state.
- **Rate limits.** The client waits out exhausted rate-limit buckets and honors
  `retry_after` on 429, and it never retries a 401 or 403. Discord temporarily bans an IP
  after 10,000 of those responses in 10 minutes.
- **A run is bounded.** A `retry_after` over 60 seconds (the global limit), a 429 that
  outlasts the retries, or a spent `max_requests` budget stops the run cleanly. It keeps
  the days it finished, reports why in `cognee_sync_stats` (`failed` plus
  `failed_rate_limit` for a rate limit; a spent budget is a planned pause, so it sets only
  `failed_budget`), and the next run starts after the last channel it finished. Deleted
  and deselected channels and threads are decided right after discovery, which always
  completes before the budget can stop a run, so they are forgotten even on a big server
  whose runs are cut short.

`cognee_sync_stats` also counts `scanned` messages, `emitted`, `unchanged` and `deleted`
days, `unreadable` channels, `empty` messages (no content at all, which points at a missing
Message Content Intent) and `skipped_no_text` (messages with nothing to render).

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
(headers, rate limits, error codes), check snowflakes and message rendering (embeds,
stickers, polls), drive the sync against a fake Discord API (incremental cursor, re-scan
window, deletions, unreadable channels and thread lists, stopped runs and resuming,
`check_active`) and through a real dlt pipeline, and run `cognee.remember` end to end to
prove a deleted thread leaves the graph.
