# cognee-community-connector-zoom

A Zoom data-source connector for [cognee](https://github.com/topoteretes/cognee): turn
your Zoom cloud recordings into memory, so you can ask "what did we decide in last
week's planning call?".

It exposes a `dlt` source you hand to `cognee.remember(...)`. Each recorded meeting
becomes one document (topic, date, duration, host, the transcript and the in-meeting
chat) that flows through cognee's normal cognify entity extraction.

## Install

```bash
uv pip install cognee-community-connector-zoom
# or, from this monorepo:
cd packages/connector/zoom && uv sync
```

## Setup

Cloud recording needs a Zoom **Pro plan or higher**. Transcripts also need **Audio
transcript** turned on under Settings > Recording.

1. In the [Zoom App Marketplace](https://marketplace.zoom.us/), choose
   **Develop > Build App > Server-to-Server OAuth App** (needs an account admin, or a
   role with permission to create these apps).
2. Add these scopes and activate the app:
   - `cloud_recording:read:list_user_recordings:admin` (classic: `recording:read:admin`)
   - `user:read:list_users:admin` (classic: `user:read:admin`), to list the account's users
   - `user:read:user:admin`, only if you pass `user_ids`
3. Copy the **Account ID**, **Client ID** and **Client Secret** into the environment,
   together with your `LLM_API_KEY` like any other cognee run:

```bash
export ZOOM_ACCOUNT_ID="..."
export ZOOM_CLIENT_ID="..."
export ZOOM_CLIENT_SECRET="..."
```

## Usage

```python
import os

import cognee
from cognee_community_connector_zoom import zoom_source

await cognee.remember(
    zoom_source(
        os.environ["ZOOM_ACCOUNT_ID"],
        os.environ["ZOOM_CLIENT_ID"],
        os.environ["ZOOM_CLIENT_SECRET"],
    ),
    dataset_name="zoom",
    primary_key="id",
    write_disposition="merge",  # REQUIRED, the add pipeline defaults to "replace"
    max_rows_per_table=0,
    self_improvement=False,
)

answer = await cognee.recall("What did we decide about the release?", datasets=["zoom"])
```

Run the same call again to sync only what changed. See `examples/example.py`.

### Choosing what to ingest

| Argument | Default | Meaning |
| --- | --- | --- |
| `user_ids` | all users | Zoom user ids or emails whose recordings to sync. By default every active and deactivated user of the account. Dropping a user later forgets that user's meetings. |
| `since` | 30 days back | First day of the meeting `start_time` window, as a date or `"YYYY-MM-DD"`. Moving it forward later forgets the meetings before it. |
| `include_transcripts` | `True` | Ingest the audio transcript (VTT). |
| `include_chat` | `True` | Ingest the in-meeting chat saved with the recording. |
| `include_direct_messages` | `False` | Also ingest the chat's direct messages (see Privacy). |
| `transcript_wait_hours` | `24` | How long a meeting whose files are not ready is held back (see below). |
| `resource_name` | `zoom_<account id>` | Staging table, which scopes this sync's state and cleanup. Use a different name per sync scope that shares a dataset. |

Video and audio files are never downloaded.

## How sync and forget-on-delete work

- **Window.** The first run stores the window start (`since`) and later runs reuse it,
  so meetings never age out on their own. Once a day, or right away when `since`
  changes, a run lists recording metadata for the whole window, one month per request
  (Zoom's limit) and 300 meetings per page, for each user. Runs in between list only
  the last month, so a run with no changes costs one request per user. Transcript and
  chat files are downloaded only for meetings that are new or whose files changed. The
  trade-off: a change to an older meeting, or a deletion, shows up within a day.
- **Meeting instances.** Recurring meetings reuse their meeting number, so each
  document is keyed by the instance `uuid` and instances never overwrite each other.
- **Transcripts that are not ready yet.** Zoom creates the transcript some minutes after
  the meeting ends, and only when cloud recording and audio transcripts are on. A
  meeting whose files are still processing, or that has a recording but no transcript
  yet, is skipped and checked again on the next run. After `transcript_wait_hours` it
  is ingested with whatever is ready, so meetings with transcripts turned off or files
  stuck in processing are not held forever; a file that is not ready is logged with the
  meeting uuid and counted in `skipped_files`. A transcript that shows up later updates
  the document.
- **Forget-on-delete.** A meeting synced before but missing from a full listing is
  removed from memory. That covers recordings you delete or move to the trash,
  recordings removed by the account's retention policy (auto-delete), and users or
  dates you take out of scope. A recording you recover from the trash comes back on the
  following sync. A listing that comes back empty as a whole while meetings were synced
  before forgets nothing until the next full listing confirms it, and an answer without
  its `meetings` or `users` list raises.
- **Expired files.** A transcript or chat file whose download answers 404 or 410 is
  skipped and counted in `skipped_files`; the meeting is still ingested.
- **A run is bounded.** 429 and 5xx are retried, honoring `Retry-After`. A `Retry-After`
  over 60 seconds (Zoom's daily limits), a 429 that outlasts the retries, more than five
  minutes of retry waits in one run, or an expired token after progress stops the run
  cleanly: it keeps what it finished and reports why in `cognee_sync_stats` (`failed`,
  plus `failed_rate_limit` or `failed_auth`). Any other failure aborts the sync and dlt
  keeps the previous state.

`cognee_sync_stats` also counts `listed`, `pending`, `unchanged`, `emitted` and
`deleted` meetings, and `skipped_files`.

## Privacy

Transcripts and chat are personal data. Nothing is fetched until you call `remember`,
and anyone with read access to the target dataset can read what was ingested, so keep
Zoom in its own dataset. Direct messages in the meeting chat (`Name to Other(Direct
Message)`) are left out unless you pass `include_direct_messages=True`, since everyone
with access to the dataset could read them. The access token is kept in memory only, is
only sent to Zoom hosts, and is never written into documents, state or error messages.

## Testing

```bash
uv run pytest tests/
```

The tests need no Zoom account. They run the client against a mocked HTTP transport
(token grant and renewal, retries and their limits, no token sent across a download
redirect or to a foreign host), check transcript and chat parsing (direct messages
included), drive the sync against a fake Zoom API that rejects date ranges over a month
(daily and recent listings, pending and stuck files, empty answers, expired files,
stopped runs, deletions) and through a real dlt pipeline (two sources in one dataset,
failed runs), and run `cognee.remember` end to end to prove a deleted recording leaves
the graph.
