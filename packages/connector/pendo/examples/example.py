import asyncio
import os

import cognee

from cognee_community_connector_pendo import pendo_source


async def main():
    key = os.environ.get("PENDO_INTEGRATION_KEY") or "mock_key"

    source = pendo_source(
        integration_key=key,
        include_guides=True,
        include_feedback=True,
        include_nps=True,
    )

    dataset_name = "pendo_product_intelligence"

    await cognee.remember(
        source,
        dataset_name=dataset_name,
    )

    query = "What features are our customers requesting most frequently in Pendo feedback?"
    results = await cognee.recall(
        query,
        dataset_name=dataset_name,
    )

    print(f"Recall results for '{query}':")
    print(results)


if __name__ == "__main__":
    asyncio.run(main())
