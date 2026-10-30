"""Example demonstrating how to ingest BigCommerce store catalog into Cognee."""

import asyncio
import os
import cognee
from cognee_community_connector_bigcommerce import bigcommerce_source


async def main() -> None:
    # 1. Initialize BigCommerce source
    source = bigcommerce_source(
        store_hash=os.getenv("BIGCOMMERCE_STORE_HASH", "abc123xyz"),
        access_token=os.getenv("BIGCOMMERCE_ACCESS_TOKEN", "mock_token"),
        include_orders=True,
        limit=50,
    )

    # 2. Add to Cognee memory
    print("Ingesting BigCommerce products and orders into Cognee...")
    await cognee.add(source)

    # 3. Cognify into knowledge graph
    print("Running Cognify extraction pipeline...")
    await cognee.cognify()

    # 4. Search store intelligence
    query = "Which products have low inventory or are priced above $100?"
    print(f"\nSearching Cognee memory: '{query}'")
    results = await cognee.search(query)
    for result in results:
        print(f"- {result}")


if __name__ == "__main__":
    asyncio.run(main())
