import asyncio
import os

import cognee
from cognee_community_connector_honeycomb import honeycomb_source


async def main() -> None:
    api_key = os.getenv("HONEYCOMB_API_KEY", "demo_api_key")

    source = honeycomb_source(
        api_key=api_key,
        include_datasets=True,
        include_boards=True,
        include_triggers=True,
        include_slos=True,
    )

    dataset_name = "observability_memory"

    await cognee.add(source, dataset_name=dataset_name)
    await cognee.cognify(dataset_name=dataset_name)

    results = await cognee.search(
        "What triggers or SLOs are defined for the payments service?",
        dataset_name=dataset_name,
    )
    print("Search results:", results)


if __name__ == "__main__":
    asyncio.run(main())
