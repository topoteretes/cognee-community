"""Hacker News connector demo — turn tracked HN topics into memory.

Pull Hacker News stories + discussion threads for your topics into cognee, with
incremental sync and forget-on-delete. ``hacker_news_source`` returns a ``dlt``
source you hand straight to ``cognee.remember`` — no routing kwargs needed.
Stories are ingested as normal documents (they flow through the full cognify
entity-extraction pipeline), via cognee's document-mode marker.

Each run snapshots the tracked window (default: last 30 days per topic):
unchanged stories keep a stable id and are not re-cognified, newly matching
stories are picked up, and stories deleted upstream drop out of the snapshot so
cognee's orphan cleanup forgets them from memory.

────────────────────────────────────────────────────────────────────────────
Privacy / opt-in
────────────────────────────────────────────────────────────────────────────
This reads *public* Hacker News content only — no account, no API key. Nothing
is fetched until you run this script. Use a dedicated dataset so you can wipe
it with a single ``cognee.prune``.

────────────────────────────────────────────────────────────────────────────
One-time setup
────────────────────────────────────────────────────────────────────────────
1. Install the extra:

       pip install "cognee-community-connector-hacker-news"

2. Export your LLM key, then run:

       export LLM_API_KEY=<your-key>
       python examples/example.py

Re-run later: new stories on your topics are picked up incrementally, and
deleted stories are reconciled out of memory.
"""

import asyncio
import os

import cognee

from cognee_community_connector_hacker_news import hacker_news_source

# Keep Hacker News in its own dataset so it is easy to inspect and forget.
DATASET_NAME = "hacker-news"

# The topics you want to track — this is how you select what gets ingested.
TOPICS = ["AI agents", "rust"]


async def main() -> None:
    if not os.environ.get("LLM_API_KEY"):
        print("Set LLM_API_KEY to run this example.")
        return

    source = hacker_news_source(
        TOPICS,
        max_stories_per_topic=10,  # keep the demo fast
        max_comments_per_story=5,
    )

    print(f"Syncing Hacker News stories for {TOPICS} into cognee ...")
    await cognee.remember(source, dataset_name=DATASET_NAME)

    answer = await cognee.search(
        query_text="What are people discussing about AI agents?",
        query_type=cognee.SearchType.GRAPH_COMPLETION,
        datasets=[DATASET_NAME],
    )
    print("\nSearch result:\n", answer)

    print(
        "\nRe-run this script later: new stories are picked up and "
        "deleted stories are reconciled out of memory."
    )


if __name__ == "__main__":
    asyncio.run(main())
