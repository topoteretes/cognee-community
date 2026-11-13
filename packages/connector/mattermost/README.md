# cognee-community-connector-mattermost

A Mattermost data-source connector for [cognee](https://github.com/topoteretes/cognee): sync
the threads of selected channels into memory, so an agent can answer "why did we roll back
the payments deploy?" without anyone digging through channel history.

It exposes a `dlt` resource you hand to `cognee.remember(...)`. Each thread (a root post
and its replies) becomes one document and goes through cognee's normal cognify pipeline,
via the document-mode marker, the same way the Notion connector works.

## Requirements

- cognee **1.6.3** or later (below 2): table-scoped document cleanup and per-row node sets.
- Python 3.10 to 3.13.
- A Mattermost server (self-hosted or cloud) with personal access tokens enabled, and an
  account that can read the channels you want to sync.

## Install

```bash
uv pip install cognee-community-connector-mattermost
# or, from this monorepo:
cd packages/connector/mattermost && uv sync
```

## Usage

```python
import cognee
from cognee_community_connector_mattermost import mattermost_source

await cognee.remember(
    mattermost_source(
        "https://chat.example.com",
        token=token,
        channels=["4xp9fdt7ojgq3m8w1a2b5c6d7e", "kz3tyy1m5fbspe8uwx2r6ra9wh"],
    ),
    dataset_name="mattermost",
    primary_key="id",
    write_disposition="merge",  # REQUIRED
    max_rows_per_table=0,
)

answers = await cognee.recall("Why did we roll back the payments deploy?", datasets=["mattermost"])
```

`write_disposition="merge"` is required: later runs only return the threads that changed,
so the default (`replace`) would wipe the others on the second sync. Settings and the token
are arguments only; `examples/example.py` reads them from `MATTERMOST_URL`,
`MATTERMOST_TOKEN` and `MATTERMOST_CHANNELS`.

| Option | Default | Meaning |
|---|---|---|
| `server_url` | required | Like `https://chat.example.com` (a path such as `/mattermost` is kept). Must be https, except a server on localhost (the Docker preview). |
| `token` | required | A personal access token, or a bot account's token. See [Setup](#setup). |
| `channels` | required | Ids of the channels to sync. Dropping one later forgets its threads. |
| `forget_archived` | `False` | Forget a selected channel's threads once it is archived. By default they stay in memory and the channel stops syncing. |
| `resource_name` | `mattermost_<host>` | Table name and key of the sync state: lowercase letters and digits with single underscores, at most 51 characters, so dlt uses it unchanged. A state written for another server is refused. Keep it fixed across runs. |
| `check_active` | `None` | Optional callable checked before every API request, before every retry wait and around every row; raising from it stops the sync. |
| `client` / `service` | `None` | A pre-built client, mainly for tests. |

After a run, `resource.cognee_sync_stats` holds counts only: `scanned` (threads written),
`deleted`, `skipped` (re-read but unchanged), `archived`, `failed`, plus `failed_channels`
or `failed_rate_limit`.

## Setup

1. **Enable personal access tokens.** They are off by default: a system admin turns them
   on under System Console → Integrations → Integration Management → *Enable Personal
   Access Tokens*. For a non-admin account, the admin also grants it under System Console →
   User Management → Users → the account → *Manage Roles* → *Allow this account to generate personal access
   tokens*.
2. **Create a token** under Profile → Security → Personal Access Tokens. A dedicated
   account that is only a member of the channels to sync keeps the token's reach small;
   the token can read whatever that account can.
3. **Find the channel ids.** Open the channel and click its name (or the ⓘ icon) to open
   the Info panel; the id is the `ID:` line under the channel handle. The connector takes
   ids, not names, because a channel can be renamed.

To try it locally, run the Docker preview (`docker run --name mattermost-preview -d
--publish 8065:8065 mattermost/mattermost-preview`), create the first account at
`http://localhost:8065` (it becomes the admin), and pass `http://localhost:8065`.

## How sync works

| Concern | Behaviour |
|---|---|
| Documents | One per thread, keyed by the root post id: the channel, then each post with its author's name and the time, oldest first. Rendering does not depend on the order the server sends posts in, so an unchanged thread keeps its content hash. Each row also carries the node set `<team>/<channel name>` (`direct_messages` for direct and group messages), so recall can be scoped to one channel. |
| First sync | The channel is read with the paged listing (200 posts a request). The cursor is the server's clock (`Date` header) from before the read, so nothing written during it is missed. |
| Later syncs | `GET /channels/{id}/posts?since=<cursor>` returns every post changed since the last sync, deleted ones included. Any change inside a thread (a reply, an edit, a deleted reply) also changes its root, so each changed thread is read again in full (`/posts/{root}/thread`). `since` is exclusive and one change can write several posts in the same millisecond, so the window starts 1 ms early and the posts already seen at the cursor are skipped. |
| More than 1000 changes | `since` returns at most 1000 posts and cannot be paged, so a full answer means changes may be missing. The channel is then read in full again, as on the first sync. |
| Deletes | A deleted root comes back through `since` with `delete_at` set and its thread is forgotten (the `_deleted` hard-delete marker, which dlt drops on `merge` and cognee's orphan cleanup removes from the graph, vector and relational stores). A deleted reply re-renders its thread without it. Every edit also leaves a hidden copy of the old version, which comes back already deleted and under its own id, so an edit never forgets anything. System events (joins, leaves, header changes) are skipped. |
| Full reads | The paged listing goes newest first by offset, so a post deleted while it is read would shift the later pages and one live post would be skipped. After a full read the connector asks what changed while it ran, and starts over when one of the posts it read was deleted. A stored thread missing from a full read is read on its own and is only forgotten if its root is really gone. |
| Archived channels | Kept in memory and no longer synced; with `forget_archived=True` their threads are forgotten. A restored channel syncs again (from scratch, if it was forgotten). |
| Dropped channels | Taking a channel out of `channels` forgets its threads on the next sync. |
| Lost access | A 403 or 404 on a channel (removed from it, or the channel was deleted) never forgets anything. The channel keeps its state and is retried next run while the others still sync; once it has failed three runs in a row the run raises with the reason. A run where every channel failed raises at once. |
| Errors | A rejected token (401), an unreachable server, a redirect or a 200 without JSON (a login or proxy page) end the run at once, and dlt keeps the previous state. 429, 5xx and dropped connections are retried, honouring the server's wait up to 30 s; a longer wait stops the rest of the run cleanly (`failed_rate_limit`) and the next run resumes. cognee holds a process-wide lock while a dlt source runs, so the connector does not sleep through long waits. |

## Resetting a sync

The cursors and thread fingerprints live in dlt's pipeline state (under `~/.dlt/pipelines`,
or `$DLT_DATA_DIR/pipelines`), not in cognee's data folders. Pruning cognee does not reset
them, so the next sync would only fetch changes. To re-sync from scratch, delete that
pipeline folder as well. The state is kept per `resource_name` and dataset
(`PIPELINE_SCOPE_ATTR`).

## Privacy

Team chat holds personal data and often internal decisions. What the connector does:

**What is read**

- Posts of the selected channels only: their text, author and time, and the channel's name.
- People are named as `First Last (@username)`. Email addresses are never read into a
  document, even though the user lookup returns them.
- Never put in a document: file contents (a file-only post shows as `(attachment)`),
  reactions, and system events.
- The connector is read-only (GET requests, plus `POST /users/ids` to look up names). It
  never posts, edits or deletes anything.

**Where it goes**

- The text above becomes cognee documents. During cognify it is sent to your configured LLM
  and embedding providers, like any document you add to cognee. Use local models if that
  matters to you.
- Anyone who can read the cognee dataset can recall what the synced channels say, including
  private channels and direct messages the account can read. Share the dataset only with
  people who may read those channels in Mattermost.
- dlt state stores the server URL, and per channel the cursor, the ids of the posts at the
  cursor, and each thread's root id with a short fingerprint. It does not store message
  text.
- Logs and error messages carry counts, HTTP statuses and endpoints with ids replaced,
  never message text or names.
- The token is an argument, is only sent to `server_url`, and is never logged or shown in a
  repr. Surrounding whitespace (a pasted newline) is stripped, and a token with whitespace
  inside is refused without echoing it. Redirects are not followed, so the token never
  reaches another host.

**How deletion is honoured**

- Deleted posts and threads are forgotten on the **next sync**.
- Dropping a channel from `channels` forgets its threads on the next sync; so does
  archiving it, with `forget_archived=True`.
- `cognee.forget(dataset=...)` alone is not enough: dlt keeps its staging copy of the rows,
  so the next sync brings forgotten threads back. Drop the channel, or delete the messages
  in Mattermost, and the next sync forgets them.

## Limitations

- Any new reply re-renders its whole thread, so a very long thread is cognified again in
  full each time it grows.
- The first sync, and any sync after more than 1000 changes in one channel, read the whole
  channel: one request per 200 posts.
- The sync state grows with the number of threads (a root id and a 16-character
  fingerprint each).
- A channel that keeps changing more than 1000 times while it is read in full is read once
  more and then kept; the connector cannot tell whether one of those changes shifted a page.
- Only message text is ingested: no file contents, link previews or message attachments
  from bots.
- A channel that was deleted for good keeps failing with a 404 until you take it out of
  `channels`, which also forgets its threads.

## Testing

```bash
uv run pytest tests/
```

No Mattermost server is needed. `tests/fake_mattermost.py` is an in-memory server with the
behaviour seen on a real one (mattermost-preview, Mattermost 11.11.1).

- `test_mattermost.py` covers rendering (names, never emails; reply order; titles; node
  sets; independent of input order), the first full read and the cursor, new replies,
  edits, deleted replies and roots, system posts, a change that keeps the text, two posts
  in the same millisecond, a full `since` page in no order, posts deleted while a channel
  is read, a channel that changes over 1000 times while it is read, stored roots missing from a listing, replies whose root was skipped, archived
  channels (kept, forgotten, restored), lost access, a channel that keeps failing or
  recovers, dropped channels, rate limits, the HTTP client (retries, dropped connections,
  long waits, redirects, a 200 without JSON, ids kept out of errors, the token), a sync
  through the real client, config validation, `check_active`, the sync stats, and real dlt
  merges into SQLite (including a failed load and two servers in one dataset).
- `test_mattermost_forget.py` runs the real cognee pipeline (LLM and embeddings mocked).
  It checks that a deleted thread and a dropped channel lose their Data records and leave
  the graph while an untouched thread keeps its id, and that a thread's node set names its
  team and channel.
