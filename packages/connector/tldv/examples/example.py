import asyncio
import os

import cognee
from cognee_community_connector_tldv import tldv_source


async def main() -> None:
    api_key = os.getenv("TLDV_API_KEY", "demo_api_key")

    source = tldv_source(
        api_key=api_key,
        limit=20,
    )

    dataset_name = "tldv_meetings_dataset"

    await cognee.add(source, dataset_name=dataset_name)
    await cognee.cognify(dataset_name=dataset_name)

    results = await cognee.search(
        "What key decisions and action items were discussed in the roadmap meeting?",
        dataset_name=dataset_name,
    )
    print("Search results:", results)


if __name__ == "__main__":
    asyncio.run(main())
