# cognee-community-connector-twist

Sync a Twist workspace into cognee memory: **threads with all their comments, and
channels**, with incremental sync and forget-on-delete.

Twist threads are long-form, and the decision in a thread usually sits several
comments down. So each thread becomes **one** cognee document (title, channel,
opening post, then every comment in order, each with its author and date), which
goes through the full cognify pipeline. You can then ask things like "what did
we decide about the checkout redesign?" next to your other sources.

## Requirements

- Python 3.11 to 3.13
- cognee 1.6.3 (document mode plus the per-row node-set column)
- A Twist workspace (the free plan works) and a Twist integration for OAuth

## Install

```bash
uv pip install cognee-community-connector-twist
# or, from this monorepo:
cd packages/connector/twist && uv sync
```

## Usage

```python
import cognee
from cognee_community_connector_twist import twist_source

await cognee.remember(
    twist_source(
        workspace_id="254427",
        access_token="...",
        # optional:
        # channel_ids=["852087"],     # default: every channel except archived ones
        # include_archived=True,      # also sync archived channels
    ),
    dataset_name="twist",
    write_disposition="merge",  # required, see below
)

print(await cognee.recall("What did we decide about checkout?", datasets=["twist"]))
```

Every argument can also come from the environment: `TWIST_WORKSPACE_ID`,
`TWIST_ACCESS_TOKEN`, `TWIST_CHANNEL_IDS` (comma separated) and
`TWIST_FULL_SYNC_EVERY`.

**Pass `write_disposition="merge"` to `remember()`.** `remember()` defaults to
`"replace"`, which overrides the source's own setting and breaks both the
incremental sync and deletes. Don't pass `primary_key`; the source already uses
`id`, which is what cognee expects for document sources.

Each row is tagged with a node set `twist:<workspace_id>:<channel_id>`, so recall
can be scoped to one channel. Direct messages and conversations are never read.

## How sync + forget-on-delete work

These rules come from testing the Twist API on a live workspace.

- **What counts as a change.** Every run lists all threads of the selected
  channels (500 per request) and compares each thread's `version`,
  `last_updated_ts`, `comment_count` and channel with the previous run. Only
  threads that changed have their comments fetched and are sent to cognee.
  `version` is what catches most changes: editing the opening post, renaming,
  deleting a comment, moving or closing a thread all move it, while
  `last_updated_ts` only moves for new comments and renames. `version` also
  moves for personal actions such as bookmarking a thread; the content hash
  keeps those from being sent to cognee again.
- **Edited comments are picked up by a sweep.** Editing a thread's latest
  comment moves its `version`, but editing an earlier comment changes nothing on
  the thread (both checked live). So the connector re-reads every thread's
  comments on the first run and every 10th run (`full_sync_every`). Force one with
  `full_sync=True`. A content hash stops unchanged threads from being processed
  again.
- **Deleted threads are forgotten** on the next sync. A thread missing from a
  complete listing of its channel gets a delete marker, and cognee's orphan
  cleanup removes it from memory. A thread moved to a channel you don't sync is
  forgotten the same way; one moved between synced channels keeps its id.
- **Deleted comments disappear** the next time their thread is rendered. Twist
  keeps them in the API with `deleted: true`; they are skipped, and so are the
  system messages Twist adds for renames, moves and closes.
- **Closed threads are kept.** The Twist app hides them, but they are not
  deleted; they stay in memory marked "closed".
- **Archived channels are opt-in.** Twist still returns archived channels, so
  this is a choice: when you sync all channels, an archived channel is skipped
  and, if it was synced before, its threads are forgotten on the next sync. Pass
  `include_archived=True`, or list the channel in `channel_ids`, to keep it.
- **A 404 is never a delete.** A deleted thread answers 404 (error 110, checked
  live), and Twist's docs use the same error for an item the token can no
  longer see. So only a thread missing from a successful channel listing is
  forgotten. When you sync all channels, a channel that no longer appears in the
  channel list has its threads forgotten; when you list channels with
  `channel_ids`, a channel the token cannot see stops the sync instead.
- **A failed sync changes nothing.** Any API error stops the run. The sync state
  is only saved when the load succeeds, and cognee only forgets things after a
  successful run, so the next sync simply retries.
- **Mass-delete guard.** A run that would forget more than half of what it has
  synced (and at least 10 items) stops, because that usually means a wrong
  workspace, channel list or token. Pass `allow_mass_delete=True` for an
  intended cleanup, or tune `max_delete_ratio`.
- **Own sync state per workspace.** Each workspace uses its own dlt pipeline
  (`PIPELINE_SCOPE_ATTR`), so its state never mixes with other dlt sources. The
  source also exposes `cognee_sync_stats` (counts only: scanned, changed,
  unchanged, deleted, skipped) and accepts an optional `check_active` callable
  that a host can use to stop a running sync.

## Setup

1. Go to <https://twist.com/app_console/create_app>, create a **General
   integration**, and on its OAuth page set the redirect URL, for example
   `http://localhost:8000/callback` (it doesn't need to be a running server).
2. Run the one-time helper to get a token and your workspace id:

   ```bash
   export TWIST_CLIENT_ID="..." TWIST_CLIENT_SECRET="..."
   uv run python examples/authorize.py
   ```

   Open the printed link, allow access, and paste the address you land on (the
   page itself will not load, which is fine). The token response is saved to
   `~/.twist_tokens.json` (mode 600). The helper asks
   only for read scopes: `workspaces:read,channels:read,threads:read,comments:read`.
3. While developing you can use the integration's **test token** from the same
   OAuth page instead. It belongs to you and is meant for testing only.

Run the full demo with `uv run python examples/example.py`. With an LLM key
(`LLM_API_KEY`) recall writes an answer; without one, cognee runs in its keyless
mode (local GLiNER extraction and fastembed embeddings, downloaded on first use)
and recall returns the matching passages.

## Limitations

- Every run lists all threads of the selected channels, so the cost grows with
  channel size (one request per 500 threads, plus one per changed thread).
- An edit to an earlier comment (not the latest one) is only seen on the next
  sweep (every 10th run by default).
- A thread's text includes its channel and workspace name. Renaming a channel
  updates the channel document right away, but its threads show the new name
  only after they change or the next sweep.
- Images are dropped from the text and links keep only their text. Attachments
  are listed by name and link but not downloaded.
- On Twist's free plan the API does not show comments and messages older than
  one month (Twist's docs: "Free plans are restricted to view data older than 1
  month"). When such a thread is re-rendered, its older comments drop out of the
  text in memory too. Use a paid workspace to keep the full history.
- Twist returns no refresh token. The access token it issued in testing was
  valid for ten years (`expires_in` 315360000). A revoked or rejected token stops
  the sync with `TwistAuthError`; run `examples/authorize.py` again for a new one.
- No rate limits are documented. 429 and 5xx responses are retried using
  `Retry-After` or exponential backoff.

## Testing

```bash
uv run pytest tests/
```

The tests use an in-memory fake of the Twist API (no network, no token),
modelled on what a live workspace returns: id-ordered thread paging, comment
windows, `version` bumps for each kind of edit, deleted comments and system
messages, archived channels, and 404s. They cover the HTTP client (errors,
retries, paging), rendering, change detection and the sweep, moves, closes,
deletes and the guards, and a real dlt pipeline run including a failed run that
must change nothing.

`tests/test_twist_forget.py` goes through cognee itself (`add` + `cognify`, with
the LLM and embeddings mocked): a deleted thread and a deleted comment must both
disappear from the knowledge graph, not just from the staging table.
