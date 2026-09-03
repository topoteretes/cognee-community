"""Sync a Readwise highlight library into a dedicated cognee dataset."""

import asyncio
import os

import cognee

from cognee_community_connector_readwise import readwise_source

DATASET_NAME = "readwise"


async def main() -> None:
    if not os.environ.get("READWISE_ACCESS_TOKEN"):
        print("Set READWISE_ACCESS_TOKEN (and LLM_API_KEY) before running this example.")
        return

    await cognee.remember(
        readwise_source(),
        dataset_name=DATASET_NAME,
        max_rows_per_table=0,
    )

    result = await cognee.search(
        query_text="What themes recur across my reading highlights?",
        query_type=cognee.SearchType.GRAPH_COMPLETION,
        datasets=[DATASET_NAME],
    )
    print(result)


if __name__ == "__main__":
    asyncio.run(main())
