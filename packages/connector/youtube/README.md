# cognee-community-connector-youtube

Sync public videos from a YouTube channel into cognee as searchable documents.

## Install

```bash
uv pip install cognee-community-connector-youtube
```

## Setup and use

Enable the YouTube Data API v3 in Google Cloud Console, create an API key, and
set `YOUTUBE_API_KEY`. Set `LLM_API_KEY` as required by cognee.

```python
import cognee
from cognee_community_connector_youtube import youtube_source

await cognee.remember(
    youtube_source(channel_id="UCxxxxxxxxxxxxxxxxxxxxxx"),
    dataset_name="youtube",
    primary_key="id",
    write_disposition="merge",
    max_rows_per_table=0,
    incremental_loading=False,
)
```

You can pass `api_key=...` and `channel_id=...` directly, or set
`YOUTUBE_API_KEY` and `YOUTUBE_CHANNEL_ID`. `include_description=False` omits
descriptions from document text; `include_captions=False` skips transcript
fetches. See [`examples/example.py`](examples/example.py) for a runnable sync
and search.

## Captions and API access

The YouTube Data API's `captions.list` and `captions.download` methods require
OAuth credentials with permission to edit the video. An API key cannot retrieve
those caption tracks. This connector uses `youtube-transcript-api` to fetch
publicly available captions through YouTube's separate transcript path. That
path does not expose private videos, may be unavailable for some videos, and
can be rate limited. Videos without captions are still ingested with their
metadata and description. Private videos and owner-only caption access require
a separate OAuth implementation.

## Sync behavior and limits

The first sync reads the channel's uploads playlist. Later syncs use YouTube's
`publishedAfter` search filter and compare the current uploads playlist with
saved video IDs so removed uploads become DLT hard-delete markers. Cognee's
normal orphan cleanup then removes them from memory. The source keeps stable
video IDs and uses DLT `merge`; keep the same channel, content selection, and
pipeline state on every run. Use a fresh pipeline if you change them. Deleting
a video may also remove it from the public uploads playlist; API
visibility changes can take time to appear.

YouTube's Data API has no `updatedAfter` filter for video metadata. This source
therefore discovers new uploads and removals, but does not detect edits to
older titles or descriptions. The `search.list` endpoint also has a bounded
result window; the uploads-playlist comparison supplements it for newly
visible videos. API quota, private-video visibility, and transcript-fetch
availability remain controlled by YouTube.

Tests mock both the Data API and transcript fetcher, so no credentials or live
YouTube requests are needed.
