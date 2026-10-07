# cognee-community-connector-telegram

A Telegram data-source connector for [cognee](https://github.com/topoteretes/cognee):
sync your group and channel messages into memory — "ask my chats".

It exposes a `dlt` resource you hand to `cognee.remember(...)` / `cognee.add(...)`.
Messages are ingested as **normal documents** (they flow through cognee's cognify
entity-extraction pipeline, not the deterministic dlt-row path), via cognee's
document-mode marker. It talks to the Bot API over plain HTTPS — no SDK needed.

## Install

```bash
uv pip install cognee-community-connector-telegram
# or, from this monorepo:
cd packages/connector/telegram && uv sync
```

## Usage

```python
import cognee
from cognee_community_connector_telegram import telegram_source

await cognee.remember(
    telegram_source(bot_token="123:ABC", chat_ids=[-1001234567890]),  # or TELEGRAM_BOT_TOKEN
    dataset_name="telegram",
)

answer = await cognee.search(
    query_text="What did the team decide about the launch?",
    query_type=cognee.SearchType.GRAPH_COMPLETION,
    datasets=["telegram"],
)
```

Scope what you ingest with `chat_ids=[...]`; omit it to ingest every chat the bot can
see. Cap a run with `limit=...`. See `examples/example.py` for the full flow.

## How sync + forget-on-delete work

The resource uses **`write_disposition="merge"`** keyed on `"<chat_id>:<message_id>"`,
so re-syncs are idempotent upserts: an edited message rewrites its row instead of
duplicating it. The **incremental cursor** is the Bot API `update_id` offset, kept in
dlt resource state — the first run backfills from offset 0, later runs fetch only
updates the bot has not seen yet.

One honest limitation: Telegram's Bot API delivers **no deletion events**, so live
sync never emits `_deleted` tombstones (the schema still carries the hard-delete
column, same contract as the Gmail/Drive connectors, for when a delete feed exists).
To forget a chat entirely, drop its dataset. Edited messages do sync.

## Setup

1. Talk to [@BotFather](https://t.me/BotFather), create a bot, and copy its token.
2. Add the bot to the groups/channels you want to ingest. For groups, disable
   **privacy mode** via @BotFather first — otherwise the bot only sees commands and
   replies, not regular messages (bots only ever see messages sent after they join).
3. Set the token as `TELEGRAM_BOT_TOKEN` (or pass `bot_token=...`), plus your
   `LLM_API_KEY` like any other cognee run.

## Testing

```bash
uv run pytest tests/
```

The tests fake the Bot API (no live token, no network) and cover update→row mapping,
chat scoping, the `update_id` incremental cursor across runs, edit upserts, retry
behavior, and the merge + hard-delete resource wiring.
