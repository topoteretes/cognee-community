"""Substack connector demo — turn a newsletter into memory.

Pull a Substack publication's RSS feed into cognee, with forget-on-delete.
``substack_source`` returns a ``dlt`` source you hand straight to
``cognee.remember`` — no routing kwargs needed. Posts are ingested as normal
documents (so they go through the full cognify entity-extraction pipeline).

Each run is a full snapshot: unchanged posts keep a stable id and are not
re-cognized, and posts that disappear from the feed (unpublished, or aged out
of the feed's recent-posts window — see the README's Known limitation) drop
out of memory on the next sync.

────────────────────────────────────────────────────────────────────────────
One-time setup
────────────────────────────────────────────────────────────────────────────
1. Install the extra:

       pip install "cognee[substack]"    # or: uv sync --extra substack

2. No authentication needed — Substack feeds are public. Export your LLM
   key, then run:

       export LLM_API_KEY="sk-..."
       uv run python examples/example.py

Re-run after the newsletter publishes a new post to see the re-sync.
"""

import asyncio
import os

import cognee

from cognee_community_connector_substack import substack_source

# Keep Substack in its own dataset so it is easy to inspect and forget.
DATASET_NAME = "substack"
PUBLICATION = "example"  # example.substack.com — replace with a real one


async def main() -> None:
    if not os.environ.get("LLM_API_KEY"):
        print("Set LLM_API_KEY to run this example.")
        return

    # Or pass feed_url="https://your-domain.com/feed" for a custom domain.
    source = substack_source(publication=PUBLICATION)

    print(f"Syncing {PUBLICATION}.substack.com into cognee ...")
    await cognee.remember(source, dataset_name=DATASET_NAME)

    answer = await cognee.search(
        query_text="Summarize what this newsletter has covered recently.",
        query_type=cognee.SearchType.GRAPH_COMPLETION,
        datasets=[DATASET_NAME],
    )
    print("\nSearch result:\n", answer)

    print("\nRe-run after the newsletter publishes to see the re-sync.")


if __name__ == "__main__":
    asyncio.run(main())
