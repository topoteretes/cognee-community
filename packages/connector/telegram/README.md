# cognee-community-connector-telegram

A Telegram data-source connector for [cognee](https://github.com/topoteretes/cognee):
sync the group and channel messages a bot can see into memory, so you can ask
"what did the team decide about the release?".

It exposes a `dlt` source you hand to `cognee.remember(...)`. Each message becomes one
document (chat, author, date, reply and forward context, text) and goes through cognee's normal
cognify pipeline via the document-mode marker, the same way the Notion connector works.

## Requirements

Python 3.10 to 3.13 and cognee 1.6.3 or later (below 2). Older 1.4 releases skip orphan
cleanup once a table becomes empty, so the last deleted record would stay in memory. 1.6.3
handles that case.

## Install

```bash
uv pip install cognee-community-connector-telegram
# or, from this monorepo:
cd packages/connector/telegram && uv sync
```

## Usage

```python
import os

import cognee
from cognee_community_connector_telegram import telegram_source

await cognee.remember(
    telegram_source(token=os.environ["TELEGRAM_BOT_TOKEN"], chats=[-1001234567890, "@my_channel"]),
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

1. Create a bot with [@BotFather](https://t.me/BotFather) (`/newbot`) and pass the token as
   `token=` (the example reads it from `TELEGRAM_BOT_TOKEN`). Use a **dedicated bot**: the
   connector consumes the bot's update queue, and `getUpdates` is unavailable while a webhook
   is set or another process polls the bot (the connector raises a clear error on the 409
   saying which, instead of deleting someone else's webhook).
2. Add the bot to the chats you want. In groups, disable privacy mode
   (`/setprivacy`, then Disable) or make the bot an admin, otherwise it only sees commands.
   In channels the bot must be an admin. The connector logs a warning when privacy mode is on.
3. Select chats with `chats=[...]` by numeric id or `@username`. `None` syncs every chat
   the bot is in, **private messages to the bot included**. Each run logs the chats it saw
   with their ids ("chats seen in this run"), so one run with `chats=None` shows what to
   pick. Dropping a chat from `chats` later leaves its messages in memory; remove the bot
   from the chat to forget them.
4. Set `LLM_API_KEY` as for any cognee run.

## How sync works

| Concern | Behaviour |
|---|---|
| One page per run | Each run makes one `getUpdates` call (up to 100 updates, `max_updates_per_run`). Asking for the next page confirms the previous one, and confirmed updates are gone, so a run never asks twice: its page is only confirmed by the next run, after this run's load succeeded. A failed load, or `check_active` stopping a run, loses nothing. A busy bot catches up over a few runs; the log says when more are waiting. |
| Incremental cursor | `update_id`, sent as `offset=last_update_id + 1`. The cursor lives in dlt resource state, which dlt only saves after a successful load, and the connector only writes it after the last row, so a failed run retries from the same point. |
| Quiet week | After a week without updates Telegram restarts `update_id` at a random value, possibly below the cursor, and an offset above the new ids would drop them. After six quiet days the connector reads without an offset, which confirms nothing. |
| Rate limits | 429 and 5xx are retried. A flood wait over a minute is not slept through: the run ends without rows or a state change, `cognee_sync_stats` reports `failed_rate_limit`, and the next sync resumes. |
| Identity | `chat_id:message_id` (message ids are only unique inside one chat). |
| Edits | `edited_message` / `edited_channel_post` carry the same id and upsert the row; unchanged messages keep a stable content hash and are not re-cognified. |
| Forget-on-delete | When the bot is removed from a chat or the chat is deleted, Telegram sends `my_chat_member` with `left`/`kicked`; every stored message of that chat is emitted with the `_deleted` hard-delete marker and cognee's `orphan_cleanup` forgets it. |
| Group to supergroup | Telegram gives the chat a new id and restarts message ids (`migrate_to_chat_id`). Old rows keep their key, the new id keeps syncing if the old one was selected, and removal forgets messages under both ids. |
| Service messages | Joins, pins and media without a caption have no text and are skipped. Media captions are kept as `[photo] caption`. |
| Several bots | Each bot writes its own table and sync state, `telegram_<bot id>` (the part of the token before `:`). Two bots in one chat see the same keys, so with one shared table, removing one bot would forget the messages the other still sees. Pass `resource_name=` (lowercase letters, digits and underscores) to choose the name and keep it fixed across runs; a sync state written by another bot is refused. A pipeline synced by the first version of this connector (one `telegram_messages` table) keeps its state and table. |
| Forwards and links | A forwarded message keeps its original author (`Forwarded from: ...`) so it is not credited to whoever forwarded it. Public chats link to `t.me/<username>/<id>`, private supergroups and channels to the members-only `t.me/c/<id>/<id>`. |

## Limitations of the Bot API

- **Per-message deletion is not observable.** Telegram sends no update when a message is
  deleted (checked on a live bot: nothing arrives, not even a gap in `update_id`), so
  deletion works per chat, as described above.
- **No history backfill.** A bot only sees messages sent after it joined.
- **Updates expire.** Unconfirmed updates are kept for about 24 hours. A run reads at most
  100 (`max_updates_per_run`), so schedule syncs often enough that a day's updates fit in
  that day's runs: hourly covers about 2,400 messages and edits a day. When a run reads a
  full page, the connector logs a warning that more are waiting.
- **Paging confirms earlier pages.** Asking Telegram for the next page of updates confirms
  the previous one, which is why a run reads a single page (see above).
- **Quiet weeks.** `update_id` can restart after a week without updates (handled, see
  above).

A user-session (MTProto) client could add history and per-message deletion, but it needs
a phone-number login and can read every chat of that account, so this connector only uses
a bot token.

## Resetting a sync

The update cursor and the stored message ids live in dlt's pipeline state (under
`~/.dlt/pipelines`, or `$DLT_DATA_DIR/pipelines`), not in cognee's data folders. Pruning
cognee does not reset them, so the next sync would only fetch changes. To re-sync from
scratch, delete that pipeline folder as well. This state is kept per bot, `resource_name`
and dataset (`PIPELINE_SCOPE_ATTR`).

## Privacy and security

- The token is passed as an argument and only sent to `api.telegram.org`. It is never
  logged and never in an error or a repr: it is part of the request path, so URLs are not
  logged and the connector's errors leave out the HTTP error that carries the URL.
- The connector is read-only: it only calls `getMe` and `getUpdates`.
- Logs name groups and channels (to help pick chat ids) but never the person behind a
  private chat.
- Hosts can pass `check_active=`, a callable run before every Bot API call and around
  every row; raising from it stops the sync without losing updates.
- After a run, `source.cognee_sync_stats` holds counts only: `messages`, `deletions`,
  `skipped`, `failed`, `failed_auth` and `failed_rate_limit`.
- Anyone who can read the cognee dataset can recall the synced messages. Share it only
  with people who may read those chats.
- Messages contain personal data, so keep them in their own dataset. To remove a chat's
  messages for good, remove the bot from that chat: the next sync forgets them, and later
  syncs don't bring them back. `cognee.forget(dataset=...)` alone is not enough: dlt keeps
  its staging copy of the rows, so the next sync brings the forgotten messages back, even
  with the pipeline folder deleted.

## Testing

```bash
uv run python -m pytest -v -p no:warnings
```

No bot token or API key is needed. `tests/fake_telegram.py` is an in-memory Bot API with
payloads shaped like the ones a real bot receives.

- `test_telegram.py` covers rendering, the cursor and replay, edits, chat selection,
  removal-driven deletion, the supergroup upgrade, one page per run, the webhook conflict, a table
  and sync state per bot, `check_active`, private-chat names kept out of logs, the dlt
  resource wiring, the row-to-document mapping in cognee, and an end-to-end dlt merge into
  SQLite that shows edits replacing rows and removed chats' rows being deleted.
- `test_telegram_delivery.py` uses a fake that drops confirmed updates like the real API, and
  checks that a failed load, `check_active` stopping a run between rows, and a failure in the
  middle of a run lose nothing; the quiet-week restart; flood waits; both kinds of 409; a 401
  without the token; a state owned by another bot; and the first version's state and table
  being kept.
- `test_telegram_forget.py` runs the real cognee pipeline (LLM and embeddings mocked, like
  the google-drive connector's forget test) and checks that text edited out of a message
  and the messages of a chat the bot was removed from leave the graph and lose their Data
  records, while an untouched message keeps its id, and that removing one of two bots from a
  shared chat leaves the other bot's messages in place.
