"""Calendly connector demo."""

import asyncio
import os

import cognee
from cognee_community_connector_calendly import calendly_source

DATASET_NAME = "calendly"


async def main() -> None:
    if not os.environ.get("CALENDLY_API_TOKEN"):
        print("Set CALENDLY_API_TOKEN to run this example.")
        return

    source = calendly_source()
    print("Syncing Calendly events into cognee ...")
    await cognee.remember(source, dataset_name=DATASET_NAME)

    answer = await cognee.search(
        query_text="What meetings do I have coming up?",
        query_type=cognee.SearchType.GRAPH_COMPLETION,
        datasets=[DATASET_NAME],
    )
    print("\\nSearch result:\\n", answer)


if __name__ == "__main__":
    asyncio.run(main())
