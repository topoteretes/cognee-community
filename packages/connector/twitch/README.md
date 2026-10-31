# cognee-community-connector-twitch

A Twitch data-source connector for [cognee](https://github.com/topoteretes/cognee):
sync the channels you follow, each channel's info, and their recent VODs and
clips into memory, so you can ask "which of the channels I follow recently
streamed about Rust, and what were those VODs about?".

It exposes a `dlt` source you hand to `cognee.remember(...)`. Each channel,
VOD and clip becomes one document and goes through cognee's normal cognify
pipeline via the document-mode marker, the same way the Notion and Telegram
connectors work. Unfollowing a channel — or a VOD expiring on Twitch —
removes it from memory.

## Requirements

Python 3.10 to 3.13, cognee 1.6.4 or later (below 2): 1.6.3 fixed orphan
cleanup of a table's last row, and 1.6.4 added per-row node sets.

## Install

```bash
uv pip install cognee-community-connector-twitch
# or, from this monorepo:
cd packages/connector/twitch && uv sync
```

## Usage

```python
import os

import cognee
from cognee_community_connector_twitch import twitch_source

await cognee.remember(
    twitch_source(
        client_id=os.environ["TWITCH_CLIENT_ID"],
        access_token=os.environ["TWITCH_ACCESS_TOKEN"],
        channels=["ruststreams"],  # optional: only these follows
    ),
    dataset_name="twitch_follows",
    primary_key="id",
    write_disposition="merge",  # REQUIRED
    max_rows_per_table=0,
    self_improvement=False,
)

answers = await cognee.recall(
    "Which of the channels I follow recently streamed about Rust?",
    datasets=["twitch_follows"],
)
```

`write_disposition="merge"` is required: every run emits the current listing
plus deletions, so the pipeline default (`replace`) would wipe everything
synced earlier.

## Setup: Client-ID and user access token

1. Register an application at
   [dev.twitch.tv/console/apps/create](https://dev.twitch.tv/console/apps/create)
   (any OAuth redirect is fine; this connector only reads). Copy the
   **Client-ID**.
2. Get a **user access token** with the `user:read:follows` scope. Visit this
   URL in a browser while logged in to Twitch, with your Client-ID in it:

   ```
   https://id.twitch.tv/oauth2/authorize?client_id=<YOUR CLIENT ID>&redirect_uri=http://localhost&response_type=token&scope=user:read:follows
   ```

   Approve the prompt. The browser lands on `http://localhost/#access_token=...`
   (a connection error page is fine — the token is in the address bar). Copy
   the `access_token` value.
3. Pass both to the connector (`client_id=`, `access_token=`) and set
   `LLM_API_KEY` as for any cognee run.

The token is validated once when the source is created, so a bad or expired
token fails immediately. User tokens from this implicit flow eventually
expire; repeat step 2 when that happens.

## What gets synced

| Row | Key | Content |
|---|---|---|
| Channel | `channel:<broadcaster id>` | name, login, the channel's description, current category, follow date, counts of VODs/clips ingested |
| VOD (archive, highlight, upload) | `video:<id>` | channel, title, full description, type, created/published dates, parsed duration (`PT2H14M33S` → `2h 14m 33s`), URL |
| Clip | `clip:<id>` | channel, title, creator, created date, URL |

Every row carries the node set `channel:<login>` (`cognee_node_set`), so a
channel and its VODs and clips link in the graph without LLM extraction.
Channel URLs are `https://www.twitch.tv/<login>`; VOD and clip URLs are the
ones Helix returns.

## How sync works

| Concern | Behaviour |
|---|---|
| Full re-list + diff | Helix has no events feed a user token can read, so every run re-lists the follows and each selected channel's VODs and clips (paginated, `first=100` pages via the `after` cursor), then diffs the keys against dlt state. Unchanged rows keep their content hash and are not re-cognified. |
| Forget-on-delete | A channel in state but not in the follows listing was unfollowed: its row and every stored VOD/clip row of it is emitted with the `_deleted` hard-delete marker, and cognee's `orphan_cleanup` forgets them. A VOD or clip in state but not in the current listing expired or was removed: its row is deleted the same way. |
| State safety | The stored keys are written to dlt resource state only after the last row of a run, and dlt persists state only after a successful load, so a failed or stopped run replays in full next time. |
| Rate limits | Helix answers with `Ratelimit-Remaining` points and a `Ratelimit-Reset` epoch (~800 points per minute; a run uses about 60). When remaining points run low the client waits until the reset before the next request; 429s are retried the same way. A wait longer than a minute is not slept through: the run stops cleanly (`failed_rate_limit` in `cognee_sync_stats`), nothing is loaded and no state moves, and the next run resumes. Rows are also built before the first one is yielded, so a stopped run emits nothing at all. |
| view_count | Deliberately excluded from the rendered content. View counts change with every view, so including them would give every VOD and clip a new content hash on every run and re-cognify the whole dataset each time. The trade-off: memory does not know how many views a VOD or clip has. |
| Selection | `channels=[login, ...]` syncs only some follows. `max_channels` (default 25) bounds a run; follows are listed most recently followed first, so the bound keeps the newest follows — raise it or use `channels=` to reach the rest. A channel that stays followed but leaves the selection keeps its rows in memory; only an unfollow forgets them. |
| Identity | `channel:<broadcaster id>` / `video:<id>` / `clip:<id>` — broadcaster ids are stable across renames, so a renamed channel upserts its row under the same key (and its rows move to the new login's node set). |
| State ownership | The sync state is scoped to `twitch:<login>` (a chosen `resource_name` scopes by name) and records the user id; a state written by another user's token is refused instead of mixing two users' rows into one table. |

## Limitations (honest ones)

- **No live-chat history.** Reading chat needs IRC or EventSub; this connector
  only uses the Helix REST API with a read-only user token.
- **VODs expire.** Twitch deletes past-broadcast VODs after a retention
  window (weeks, depending on the channel). When one vanishes from the
  listing, the connector forgets it — that is by design, but it means memory
  of old streams fades with the VODs.
- **The clips listing is a top-list.** Helix returns a broadcaster's clips in
  descending view-count order, roughly 1,000 across all pages. A clip that
  falls out of the top listing is treated as removed and forgotten here,
  even if it still exists on Twitch.
- **Full re-list every run.** Without events, change detection costs one
  listing of follows plus two listings per selected channel per run — cheap
  for tens of channels, noticeable for hundreds (`max_channels` defaults to
  25 partly for this).
- **Token renewal is manual.** Implicit-flow user tokens expire; re-do the
  authorize step when syncs start failing with `TwitchAuthError`.

## Resetting a sync

The stored keys live in dlt's pipeline state (`~/.dlt/pipelines`, or
`$DLT_DATA_DIR/pipelines`), not in cognee's data folders. Pruning cognee does
not reset them. To re-sync from scratch, delete that pipeline folder too;
the state is kept per user (or `resource_name`) and dataset.

## Privacy and security

- The access token is passed as an argument and only sent to
  `api.twitch.tv`. It is never logged and never in an error or a repr.
- The connector is read-only: `GET /users`, `GET /channels/followed`,
  `GET /channels`, `GET /videos`, `GET /clips` — nothing else, and the token
  only carries the `user:read:follows` scope.
- Hosts can pass `check_active=`, a callable run before every Helix call and
  around every row; raising from it stops the sync without losing anything.
- After a run, `source.cognee_sync_stats` holds counts only: `items`,
  `deletions`, `skipped`, `failed`, `failed_auth`, `failed_rate_limit`.
- Anyone who can read the cognee dataset can recall what you watch. To
  forget a channel for good, unfollow it: the next sync forgets it and its
  content, and later syncs don't bring them back. `cognee.forget(dataset=...)`
  alone is not enough — dlt keeps its staging copy, so the next sync would
  re-add everything still followed.

## Testing

```bash
uv run pytest tests/ -q
```

No token, Client-ID or network is needed. `tests/fake_twitch.py` is an
in-memory Helix with payloads shaped like the reference documentation:
three follows (one with an archive, a highlight and an upload; one with
clips; one bare), pagination through the `after` cursor, a 401, a 429, and
rate-limit waits on both sides of a minute.

- `test_twitch.py` covers rendering (durations, determinism, node sets,
  `view_count` exclusion), the full listing with pagination, unfollow and
  expiry deletions, new VODs and renames, the `channels=` filter and
  `max_channels`, rate-limit behaviour (real client, injected HTTP), 401s
  without the token, and the dlt resource wiring (merge, PK, hard delete,
  node-set column, document path, stats, `check_active`).
- `test_twitch_delivery.py` drives a real dlt pipeline into SQLite and
  checks that unfollowed and expired rows physically disappear from the
  destination, that a long rate-limit wait stops cleanly and replays, that a
  run stopped by `check_active` loses nothing, and that a sync state written
  by another user is refused.
