"""YouTube connector demo — turn a channel's uploads into memory.

Pull a YouTube channel's public videos (titles, descriptions, publish dates)
into cognee, incrementally and with forget-on-delete. ``youtube_source``
returns a ``dlt`` resource you hand straight to ``cognee.remember``. Rows are
ingested as normal documents (so they go through the full cognify
entity-extraction pipeline).

The first run is a full backfill; every later run fetches only uploads
published after the last sync and propagates deletions, so videos you delete
(or make private) disappear from memory on the next sync.

────────────────────────────────────────────────────────────────────────────
Privacy / opt-in
────────────────────────────────────────────────────────────────────────────
This reads the public metadata of the channel you point it at. Nothing is
fetched until you run this script. Use a dedicated dataset so you can wipe it
with a single ``cognee.forget``. Mind your API key's daily quota.

────────────────────────────────────────────────────────────────────────────
One-time setup
────────────────────────────────────────────────────────────────────────────
1. Install the extra:

       pip install "cognee[youtube]"      # or: uv sync --extra youtube

2. In Google Cloud Console: enable the *YouTube Data API v3*, create an API
   key (Credentials → API key).
3. Export them and your LLM key, then run:

       export YOUTUBE_API_KEY="..."
       export YOUTUBE_CHANNEL_ID="UC..."
       export LLM_API_KEY="sk-..."
       uv run python packages/connector/youtube/examples/example.py

Re-run after deleting a video to see the incremental re-sync and
forget-on-delete.
"""

import asyncio

import cognee

from cognee_community_connector_youtube import youtube_source


async def main():
    source = youtube_source()  # reads YOUTUBE_API_KEY / YOUTUBE_CHANNEL_ID

    print("=== Initial sync (full backfill) ===")
    result = await cognee.remember(
        source,
        dataset_name="youtube_demo",
        primary_key="id",
        # "merge" is required: it's what makes re-runs incremental and what
        # makes deletions propagate via orphan cleanup. The default
        # ("replace") would wipe the synced corpus on every run.
        write_disposition="merge",
        # The DLT ingestion default caps a table at 50 rows; real channels
        # exceed that, so lift the cap.
        max_rows_per_table=0,
    )
    print(result)

    answer = await cognee.recall("What did I publish about feature X?")
    print("Recall:", answer)

    print("\n=== Incremental re-sync (only new uploads/deletions are processed) ===")
    result = await cognee.remember(
        youtube_source(),
        dataset_name="youtube_demo",
        primary_key="id",
        write_disposition="merge",
        max_rows_per_table=0,
    )
    print(result)


if __name__ == "__main__":
    asyncio.run(main())
