"""Raindrop connector demo."""

import asyncio
import os

import cognee
from cognee_community_connector_raindrop import raindrop_source

DATASET_NAME = "raindrop"


async def main() -> None:
    if not os.environ.get("RAINDROP_API_TOKEN"):
        print("Set RAINDROP_API_TOKEN to run this example.")
        return

    source = raindrop_source()
    print("Syncing Raindrop bookmarks into cognee ...")
    await cognee.remember(source, dataset_name=DATASET_NAME)

    answer = await cognee.search(
        query_text="What topics do I bookmark most?",
        query_type=cognee.SearchType.GRAPH_COMPLETION,
        datasets=[DATASET_NAME],
    )
    print("\\nSearch result:\\n", answer)


if __name__ == "__main__":
    asyncio.run(main())
