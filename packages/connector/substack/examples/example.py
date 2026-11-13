"""Substack connector demo - turn a newsletter into memory.

Pull a Substack publication's posts into cognee, with forget-on-delete.
``substack_source`` returns a ``dlt`` source you hand straight to
``cognee.remember``. Posts are ingested as normal documents (full cognify
entity-extraction pipeline). No auth is needed: it reads the public RSS feed.

Each run is a full snapshot of the feed: unchanged posts keep a stable id and are
not re-cognified, new posts are picked up, and posts that disappear from the feed
are forgotten by cognee's orphan cleanup on the next sync.

Paywalled posts only expose a preview in RSS; they are ingested with an
``is_partial`` marker and a visible "preview only" note.

Setup:

    uv sync        # or: pip install -e .
    export LLM_API_KEY="sk-..."
    export SUBSTACK_PUBLICATION="platformer"   # name, host or feed URL
    uv run python examples/example.py
"""

import asyncio
import os

import cognee

from cognee_community_connector_substack import substack_source

# Keep Substack in its own dataset so it is easy to inspect and forget.
DATASET_NAME = "substack"


async def main() -> None:
    publication = os.environ.get("SUBSTACK_PUBLICATION")
    if not publication:
        print("Set SUBSTACK_PUBLICATION (e.g. 'platformer') to run this example.")
        return

    print(f"Syncing {publication} into cognee ...")
    await cognee.remember(substack_source(publication), dataset_name=DATASET_NAME)

    answer = await cognee.search(
        query_text="What are the recent posts about?",
        query_type=cognee.SearchType.GRAPH_COMPLETION,
        datasets=[DATASET_NAME],
    )
    print("\nSearch result:\n", answer)
    print("\nRe-run later: new posts sync in, and unpublished posts are forgotten.")


if __name__ == "__main__":
    asyncio.run(main())
