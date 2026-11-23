"""Substack connector demo."""

import asyncio
import os

import cognee
from cognee_community_connector_substack import substack_source

DATASET_NAME = "substack"


async def main() -> None:
    feed = os.environ.get("SUBSTACK_FEED_URL")
    if not feed:
        print("Set SUBSTACK_FEED_URL to run this example.")
        return

    source = substack_source(feed_url=feed)
    print("Syncing Substack posts into cognee ...")
    await cognee.remember(source, dataset_name=DATASET_NAME)

    answer = await cognee.search(
        query_text="What topics has this newsletter covered recently?",
        query_type=cognee.SearchType.GRAPH_COMPLETION,
        datasets=[DATASET_NAME],
    )
    print("\\nSearch result:\\n", answer)


if __name__ == "__main__":
    asyncio.run(main())
