"""Stack Overflow connector demo."""

import asyncio
import os

import cognee
from cognee_community_connector_stackoverflow import stackoverflow_source

DATASET_NAME = "stackoverflow"


async def main() -> None:
    # Tags are required to stay within quota
    source = stackoverflow_source(tags=["python", "ai"])
    print("Syncing Stack Overflow posts into cognee ...")
    await cognee.remember(source, dataset_name=DATASET_NAME)

    answer = await cognee.search(
        query_text="What are common patterns for building AI agents in Python?",
        query_type=cognee.SearchType.GRAPH_COMPLETION,
        datasets=[DATASET_NAME],
    )
    print("\\nSearch result:\\n", answer)


if __name__ == "__main__":
    asyncio.run(main())
