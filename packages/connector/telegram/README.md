# cognee-community-connector-telegram

A Telegram data-source connector for [cognee](https://github.com/topoteretes/cognee):
sync group and channel messages seen by your bot into memory — "ask my chats".

It exposes a `dlt` resource you hand to `cognee.remember(...)`. Messages are ingested as
**normal documents** (they flow through cognee's cognify entity-extraction pipeline, not
the deterministic dlt-row path) via cognee's document-mode marker. Auth is a **bot token**
from @BotFather — no OAuth dance, no user session; the connector is read-only
(`getUpdates` is the only Bot API method it calls, over the standard library — no
Telegram client dependency).

## Install

```bash
uv pip install cognee-community-connector-telegram
# or, from this monorepo:
cd packages/connector/telegram && uv sync
```

## Setup (Bot API specifics — read this first)

1. Create a bot with [@BotFather](https://t.me/BotFather) (`/newbot`) and export the token:

   ```bash
   export TELEGRAM_BOT_TOKEN="123456:ABC-DEF..."
   ```

2. Add the bot to the groups/channels you want to ingest (for channels it must be an
   admin — Telegram only delivers `channel_post` updates to admins).
3. **Group privacy mode**: by default a bot in a group only receives commands and
   replies to itself. To ingest the actual conversation, disable privacy mode in
   @BotFather (`/setprivacy` → Disable), then **re-add the bot** to existing groups
   (Telegram applies the setting on join).
4. **Bots only see messages sent after they join.** There is no history backfill on
   this auth model — the connector ingests the stream from the moment the bot is in
   the chat, which is exactly what the incremental sync consumes.
5. Do not run another `getUpdates` consumer (or a webhook) against the same token —
   Telegram allows only one update stream per bot.

## Usage

```python
import cognee
from cognee_community_connector_telegram import telegram_source

await cognee.remember(
    # or telegram_source() to ingest every chat the bot can see
    telegram_source(chats=["@my_channel", -1001234567890]),
    dataset_name="telegram",
    primary_key="id",
    write_disposition="merge",  # incremental upsert by chat/message id
    max_rows_per_table=0,  # 0 = no row cap (busy chats exceed the default 50)
)

answer = await cognee.search(
    query_text="What did the team decide about the launch?",
    query_type=cognee.SearchType.GRAPH_COMPLETION,
    datasets=["telegram"],
)
```

See `examples/example.py` for the full flow.

## What gets ingested

- `message` and `channel_post` updates whose chat passes the optional `chats` filter
  (numeric chat ids and/or `@usernames`, matched case-insensitively; omitted = all
  chats the bot receives updates for).
- **Text or media caption** becomes the content. Sender, date, chat, reply-to and
  forward origin become record metadata (a `metadata` JSON column, also folded into
  the message text as a `Message context` section so it survives entity extraction —
  which is what turns sender→message and reply chains into graph edges).
- Media files themselves are **not** downloaded — for a captioned photo/video/document
  the caption is ingested with an `attached media:` note. Service messages and media
  without a caption carry no prose to cognify and are skipped.
- The row id is `"{chat_id}/{message_id}"` (Telegram message ids are only unique per
  chat). The provenance `url` is a real `https://t.me/...` link for public chats and
  private supergroups/channels; basic private groups have no web link, so a
  deterministic `telegram://chat/.../message/...` identifier stands in.

## How incremental sync works

The cursor is the Bot API's own **`update_id` offset**, kept in dlt's per-resource
state: each run calls `getUpdates` starting from the offset committed by the previous
run, so only what changed since the last run is fetched and processed.

**Edits re-sync.** Telegram delivers edits as `edited_message` / `edited_channel_post`
updates; the edited row upserts on `merge` under the same id. Re-emission is gated on a
content hash of the rendered row, so an edit signal that did not change the text (e.g.
an inline-keyboard update) does not re-cognify the message.

One consequence of the offset protocol worth knowing: requesting updates past an
`update_id` confirms the earlier ones to Telegram. If a run crashes between fetching a
later page and committing the load, updates from earlier pages of that same run can be
lost (Telegram drops confirmed updates; unconfirmed ones are kept for about 24 hours,
so also don't let the bot sit unsynced for longer than that). Within a committed run
this is airtight: replayed updates are deduplicated by the content-hash map.

## How forget-on-delete works — honest limits

**Deleting a single message is invisible to a bot.** The Bot API sends no update when a
message is deleted, and bots cannot re-read chat history to diff against — so
per-message deletions are **not** detectable on the bot-token auth model this issue
asks for. We document that here instead of pretending otherwise. (Detecting them would
require a user-session client such as Telethon — a different auth model with its own
issue-sized tradeoffs.)

**Removing the source is detected.** What Telegram *does* reliably signal is the bot
losing access to a chat: a `my_chat_member` update with status `left` or `kicked`. On
that signal the connector emits a `_deleted` hard-delete tombstone for **every known
message of that chat**; dlt removes those rows on `merge` and cognee's existing
`orphan_cleanup` purges them from the graph, vector, and relational stores on the next
sync. So deleting the source upstream — removing the bot from the chat, or deleting
the group (Telegram kicks the bot) — removes its records from the graph on the next
sync, per the acceptance criteria.

## Testing

```bash
uv run --with pytest pytest tests/
```

The tests need no live Telegram: the sync core is pure over
`(config, state, updates)` and the pipeline tests inject a fake `get_updates` client.
They cover config normalization, chat filtering, message rendering (captions, urls,
reply/forward metadata), the `update_id` cursor (including crash-replay dedup), edit
re-emission by content hash, forget-on-removal tombstones, and the dlt merge +
hard-delete pipeline end to end.
