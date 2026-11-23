"""Readwise connector demo."""

import asyncio
import os

import cognee
from cognee_community_connector_readwise import readwise_source

DATASET_NAME = "readwise"


async def main() -> None:
    if not os.environ.get("READWISE_API_TOKEN"):
        print("Set READWISE_API_TOKEN to run this example.")
        return

    source = readwise_source()
    print("Syncing Readwise highlights into cognee ...")
    await cognee.remember(source, dataset_name=DATASET_NAME)

    answer = await cognee.search(
        query_text="What ideas keep appearing in my highlights?",
        query_type=cognee.SearchType.GRAPH_COMPLETION,
        datasets=[DATASET_NAME],
    )
    print("\\nSearch result:\\n", answer)


if __name__ == "__main__":
    asyncio.run(main())
