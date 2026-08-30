"""RSS / Atom connector demo — turn feeds into memory.

Pull public feeds into cognee, with forget-on-delete. ``rss_source`` returns a
``dlt`` source you hand straight to ``cognee.remember`` — no routing kwargs
needed. Entries are ingested as normal documents.

Each run is a full snapshot: unchanged items keep a stable id and are not
re-cognified, and items that disappear from the feed drop out of the snapshot
so cognee's orphan cleanup forgets them on the next sync.

Privacy / opt-in: nothing is fetched until you run this script. Use a dedicated
dataset so you can wipe it with ``cognee.prune``.
"""

import asyncio
import os

import cognee

from cognee_community_connector_rss import rss_source

DATASET_NAME = "rss"
DEFAULT_FEEDS = [
    "https://blog.python.org/feeds/posts/default",
]


async def main() -> None:
    feeds = [url.strip() for url in os.environ.get("RSS_FEED_URLS", "").split(",") if url.strip()]
    if not feeds:
        feeds = DEFAULT_FEEDS
        print(f"RSS_FEED_URLS unset; using {feeds[0]}")

    source = rss_source(feeds)

    print("Syncing RSS / Atom entries into cognee ...")
    await cognee.remember(source, dataset_name=DATASET_NAME)

    answer = await cognee.search(
        query_text="Summarize the latest posts.",
        query_type=cognee.SearchType.GRAPH_COMPLETION,
        datasets=[DATASET_NAME],
    )
    print("\nSearch result:\n", answer)
    print(
        "\nRemove an item from the feed and re-run: missing entries are "
        "reconciled out of memory."
    )


if __name__ == "__main__":
    asyncio.run(main())
