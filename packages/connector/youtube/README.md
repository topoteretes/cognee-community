# cognee-community-connector-youtube

A YouTube data-source connector for [cognee](https://github.com/topoteretes/cognee):
sync YouTube videos, playlists, or channels into memory — "ask my YouTube channel".

It exposes a `dlt` source you hand to `cognee.remember(...)` / `cognee.add(...)`.
YouTube videos are ingested as **normal documents** (they flow through cognee's
cognify entity-extraction pipeline, not the deterministic dlt-row path) via cognee's
`DOCUMENT_SOURCE_ATTR` document-mode marker.

## Features

- **Metadata, Descriptions, and Captions**: Ingests title, description, and transcripts/captions
  into a unified document content representation, with structured columns (`channel_id`, `published_at`,
  `duration_seconds`, `view_count`, `tags`, etc.).
- **Efficient Enumeration**: Uses `playlistItems.list` and batched `videos.list` (up to 50 videos per call),
  avoiding expensive `search.list` operations (1 quota unit vs 100 quota units).
- **Graceful Captions Retrieval**: Fetches transcripts using `youtube-transcript-api` for public
  manual/auto captions without OAuth complexity. Videos without captions are ingested cleanly without errors.
- **Quota Pacing**: Moving-window `YouTubeQuota` limiter prevents bursts and stays within the 10,000 units/day free tier.
- **Incremental Sync**:
  - `published_after` filter: Skips videos published prior to cursor timestamps.
  - Etag and content-hash watermark: Persisted in dlt `resource_state`. Unchanged videos skip caption
    fetching completely (0 extra caption API calls). Videos whose changes are limited to volatile statistics
    (e.g., view count changes) reuse cached captions.
- **Forget-on-Delete**:
  - `replace` mode (default for channels/playlists): Videos removed or made private fall out of the snapshot,
    and cognee's `orphan_cleanup` purges them from the knowledge graph and vector stores.
  - `merge` mode (default for explicit `video_ids`): Validates tracked IDs; deleted or private videos
    emit `_deleted=True` hard-delete tombstones.

## Install

```bash
uv pip install cognee-community-connector-youtube
# or, from this monorepo:
cd packages/connector/youtube && uv sync
```

## Setup

1. Obtain a YouTube Data API v3 API key from the [Google Cloud Console](https://console.cloud.google.com/apis/credentials).
2. Set `YOUTUBE_API_KEY` (or pass `api_key=...`), plus your LLM API key:

```bash
export YOUTUBE_API_KEY="AIzaSy..."
export LLM_API_KEY="sk-..."
```

## Usage

```python
import cognee
from cognee_community_connector_youtube import youtube_source

# Ingest an entire channel
await cognee.remember(
    youtube_source(channel_id="UC_x5XG1OV2P6uZZ5FSM9Ttw"),
    dataset_name="youtube",
)

# Or ingest a specific playlist
await cognee.remember(
    youtube_source(playlist_id="PLrAXtmErZgOdP_8GztsuKi9upUDIRtxp4"),
    dataset_name="youtube",
)

# Or track specific videos
await cognee.remember(
    youtube_source(video_ids=["dQw4w9WgXcQ", "jNQXAC9IVRw"]),
    dataset_name="youtube",
)

# Query knowledge graph
answer = await cognee.search(
    query_text="What are the key topics discussed across the videos?",
    query_type=cognee.SearchType.GRAPH_COMPLETION,
    datasets=["youtube"],
)
```

See `examples/example.py` for a full runnable example.

## Testing

```bash
cd packages/connector/youtube
uv run --with pytest pytest tests/
```

All tests are fully mocked (no live API key or network access required).
