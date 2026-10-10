# cognee-community-connector-telegram

A Telegram data-source connector for [cognee](https://github.com/topoteretes/cognee):
sync the group and channel messages a bot can see into memory, so you can ask
"what did the team decide about the release?".

It exposes a `dlt` source you hand to `cognee.remember(...)`. Each message becomes one
document (chat, author, date, reply and forward context, text) and goes through cognee's normal
cognify pipeline via the document-mode marker, the same way the Notion connector works.

## Requirements

cognee 1.6.3 (pinned). Older 1.4 releases skip orphan cleanup once a table becomes empty,
so the last deleted record would stay in memory. 1.6.3 handles that case.

## Install

```bash
uv pip install cognee-community-connector-telegram
# or, from this monorepo:
cd packages/connector/telegram && uv sync --all-extras
```

## Usage

```python
import cognee
from cognee_community_connector_telegram import telegram_source

await cognee.remember(
    telegram_source(chats=[-1001234567890, "@my_channel"]),  # TELEGRAM_BOT_TOKEN from env
    dataset_name="telegram_chats",
    primary_key="id",
    write_disposition="merge",  # REQUIRED
    max_rows_per_table=0,
    self_improvement=False,
)

answers = await cognee.recall(
    "What did we decide about the release date?", datasets=["telegram_chats"]
)
```

`write_disposition="merge"` is required: `getUpdates` only returns new updates, so the
pipeline default (`replace`) would wipe earlier messages on the second sync.

## Setup

1. Create a bot with [@BotFather](https://t.me/BotFather) (`/newbot`) and set the token as
   `TELEGRAM_BOT_TOKEN`. Use a **dedicated bot**: the connector consumes the bot's update
   queue, and `getUpdates` is unavailable while a webhook is set (the connector raises a
   clear error on the 409 instead of deleting someone else's webhook).
2. Add the bot to the chats you want. In groups, disable privacy mode
   (`/setprivacy`, then Disable) or make the bot an admin, otherwise it only sees commands.
   In channels the bot must be an admin.
3. Select chats with `chats=[...]` by numeric id or `@username`. `None` syncs every chat
   the bot is in. Each run logs the chats it saw with their ids
   ("chats seen in this run"), so one run with `chats=None` shows what to pick.
4. Set `LLM_API_KEY` as for any cognee run.

## How sync works

| Concern | Behaviour |
|---|---|
| Incremental cursor | `update_id`. Each run calls `getUpdates(offset=last_update_id + 1)`. The cursor lives in dlt resource state, which dlt only saves after a successful load, so a failed run retries from the same point. |
| Identity | `chat_id:message_id` (message ids are only unique inside one chat). |
| Edits | `edited_message` / `edited_channel_post` carry the same id and upsert the row; unchanged messages keep a stable content hash and are not re-cognified. |
| Forget-on-delete | When the bot is removed from a chat or the chat is deleted, Telegram sends `my_chat_member` with `left`/`kicked`; every stored message of that chat is emitted with the `_deleted` hard-delete marker and cognee's `orphan_cleanup` forgets it. |
| Group to supergroup | Telegram gives the chat a new id and restarts message ids (`migrate_to_chat_id`). Old rows keep their key, the new id keeps syncing if the old one was selected, and removal forgets messages under both ids. |
| Service messages | Joins, pins and media without a caption have no text and are skipped. Media captions are kept as `[photo] caption`. |
| Forwards and links | A forwarded message keeps its original author (`Forwarded from: ...`) so it is not credited to whoever forwarded it. Public chats link to `t.me/<username>/<id>`, private supergroups and channels to the members-only `t.me/c/<id>/<id>`. |

## Limitations of the Bot API

- **Per-message deletion is not observable.** Telegram sends no update when a message is
  deleted (checked on a live bot: nothing arrives, not even a gap in `update_id`), so
  deletion works per chat, as described above.
- **No history backfill.** A bot only sees messages sent after it joined.
- **Updates expire.** Unconfirmed updates are kept for about 24 hours, so sync at least
  daily.
- **Paging confirms earlier pages.** Asking Telegram for the next page of updates confirms
  the previous one. If a run fetches several pages and the load then fails, the earlier
  pages are not delivered again. Pass `max_updates_per_run=100` to fetch one page per run
  when that matters more than throughput.

A user-session (MTProto) client could add history and per-message deletion, but it needs
a phone-number login and can read every chat of that account, so this connector only uses
a bot token.

## Resetting a sync

The update cursor and the stored message ids live in dlt's pipeline state (under
`~/.dlt/pipelines`, or `$DLT_DATA_DIR/pipelines`), not in cognee's data folders. Pruning
cognee does not reset them, so the next sync would only fetch changes. To re-sync from
scratch, delete that pipeline folder as well. On cognee 1.6+ this state is kept per bot and
dataset (`PIPELINE_SCOPE_ATTR`).

## Privacy and security

- The token is read from the environment or an argument and only sent to
  `api.telegram.org`; it is never logged (it is part of the request path, so URLs are not
  logged either).
- The connector is read-only: it only calls `getMe` and `getUpdates`.
- Messages contain personal data. Keep them in their own dataset so you can remove them
  with one `cognee.forget(dataset=...)` call.

## Testing

```bash
uv run pytest tests/
```

The tests mock the Bot API (no token, no network) with payloads shaped like the ones a
real bot receives, and cover rendering, the cursor and replay, edits, chat selection,
removal-driven deletion, the supergroup upgrade, paging, the webhook conflict, the dlt
resource wiring, the row-to-document mapping in cognee, and an end-to-end dlt merge into
SQLite that shows edits replacing rows and removed chats' rows being deleted.
