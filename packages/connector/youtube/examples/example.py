"""YouTube connector demo — sync YouTube video content into memory.

Pull YouTube video metadata, descriptions, and transcripts/captions into cognee,
with incremental sync and forget-on-delete. ``youtube_source`` returns a ``dlt``
source that you pass directly to ``cognee.remember`` — no manual routing kwargs needed.

Videos are ingested as normal documents (so they flow through the complete cognify
entity-extraction and knowledge graph pipeline).

Sync & Deletion modes:
1. Channel / Playlist (write_disposition="replace", default for channels):
   Each run creates a snapshot of live videos. When a video is deleted or made
   private, it drops out of the snapshot, and cognee's orphan cleanup reconciles it.
2. Tracked Video IDs (write_disposition="merge", default for video_ids):
   Monitors specific videos; deleted/private videos emit hard-delete markers.

Incremental sync utilizes an etag + content-hash watermark in dlt state so caption
fetching is skipped on re-sync if the video content has not changed.

────────────────────────────────────────────────────────────────────────────
One-time setup
────────────────────────────────────────────────────────────────────────────
1. Install the connector:

       cd packages/connector/youtube && uv sync

2. Obtain a YouTube Data API v3 key from Google Cloud Console.
3. Export your keys and run:

       export YOUTUBE_API_KEY="AIzaSy..."
       export LLM_API_KEY="sk-..."
       uv run python examples/example.py
"""

import asyncio
import os

import cognee

from cognee_community_connector_youtube import youtube_source

DATASET_NAME = "youtube"


async def main() -> None:
    if not os.environ.get("YOUTUBE_API_KEY"):
        print("Please set YOUTUBE_API_KEY in your environment to run this example.")
        return

    # Ingest a channel (or specify playlist_id=... or video_ids=[...])
    # Example channel ID (e.g., Google Developers or your own channel)
    channel_id = os.environ.get("YOUTUBE_CHANNEL_ID", "UC_x5XG1OV2P6uZZ5FSM9Ttw")

    print(f"Syncing YouTube channel {channel_id} into cognee ...")
    source = youtube_source(channel_id=channel_id)

    await cognee.remember(source, dataset_name=DATASET_NAME)

    print("\nQuerying cognee knowledge graph for ingested YouTube videos ...")
    answer = await cognee.search(
        query_text="Summarize the topics covered in these YouTube videos.",
        query_type=cognee.SearchType.GRAPH_COMPLETION,
        datasets=[DATASET_NAME],
    )
    print("\nSearch result:\n", answer)


if __name__ == "__main__":
    asyncio.run(main())
