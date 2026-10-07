import asyncio
import os

import cognee

from cognee_community_connector_missive import missive_source

DATASET_NAME = "missive"


async def main() -> None:
    if not os.environ.get("MISSIVE_API_TOKEN"):
        print("Set MISSIVE_API_TOKEN to run this example.")
        return

    source = missive_source()

    print("Syncing Missive conversations into cognee...")
    await cognee.remember(source, dataset_name=DATASET_NAME)

    answer = await cognee.search(
        query_text="Summarize recent customer support issues and team decisions.",
        query_type=cognee.SearchType.GRAPH_COMPLETION,
        datasets=[DATASET_NAME],
    )
    print("\nSearch result:\n", answer)


if __name__ == "__main__":
    asyncio.run(main())
