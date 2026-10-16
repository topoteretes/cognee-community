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
import cognee
from cognee_community_connector_zoom import zoom_source

await cognee.remember(
    zoom_source(),  # credentials from the ZOOM_* environment variables
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
| `user_ids` | all users | Zoom user ids or emails whose recordings to sync. By default every active and deactivated user of the account. |
| `since` | 30 days back | First day of the meeting `start_time` window, as a date or `"YYYY-MM-DD"`. |
| `include_transcripts` | `True` | Ingest the audio transcript (VTT). |
| `include_chat` | `True` | Ingest the in-meeting chat saved with the recording. |
| `transcript_wait_hours` | `24` | How long a recording without a transcript is held back (see below). |
| `resource_name` | `zoom_meetings` | Staging table. Use a different name per sync scope that shares a dataset. |

Video and audio files are never downloaded.

## How sync and forget-on-delete work

- **Window.** The first run stores the window start (`since`) and later runs reuse it,
  so meetings never age out on their own. Each run lists recording metadata for the
  whole window, one month per request (Zoom's limit), for each user. Transcript and
  chat files are downloaded only for meetings that are new or whose files changed.
  Listing cost grows with users x months in the window, so narrow `user_ids` or `since`
  on big accounts.
- **Meeting instances.** Recurring meetings reuse their meeting number, so each
  document is keyed by the instance `uuid` and instances never overwrite each other.
- **Transcripts that are not ready yet.** Zoom creates the transcript some minutes after
  the meeting ends, and only when cloud recording and audio transcripts are on. A
  meeting whose files are still processing, or that has a recording but no transcript
  yet, is skipped and checked again on the next run. After `transcript_wait_hours` it
  is ingested with whatever exists (metadata and chat), so meetings with transcripts
  turned off are not stuck. A transcript that shows up later updates the document.
- **Forget-on-delete.** A meeting synced before but missing from the window listing is
  removed from memory on the next sync. That covers recordings you delete or move to
  the trash, recordings removed by the account's retention policy (auto-delete), and
  users or dates you take out of scope. A recording you recover from the trash comes
  back on the following sync.
- **Failures.** Any request that still fails after retries (429 and 5xx are retried,
  honoring `Retry-After`) aborts the sync. dlt then keeps the previous state, so a
  partial listing can never look like a deletion.

## Privacy

Transcripts and chat are personal data. Nothing is fetched until you call `remember`,
and anyone with read access to the target dataset can read what was ingested, so keep
Zoom in its own dataset. The access token is kept in memory only and is never written
into documents, state or error messages.

## Testing

```bash
uv run pytest tests/
```

The tests need no Zoom account. They run the client against a mocked HTTP transport
(token grant and renewal, retries, no token sent across a download redirect), check
transcript and chat parsing, drive the sync against a fake Zoom API through a real dlt
pipeline (incremental cursor, pending transcripts, deletions, failed runs), and run
`cognee.remember` end to end to prove a deleted recording leaves the graph.
