"""Example demonstrating how to ingest YNAB personal finances into Cognee."""

import asyncio
import os
import cognee
from cognee_community_connector_ynab import ynab_source


async def main() -> None:
    # 1. Initialize YNAB source using personal access token
    source = ynab_source(
        access_token=os.getenv("YNAB_ACCESS_TOKEN", "mock_token"),
        since_date="2026-01-01",
    )

    # 2. Add to Cognee memory
    print("Ingesting YNAB transactions and budget categories into Cognee...")
    await cognee.add(source)

    # 3. Cognify into knowledge graph
    print("Running Cognify extraction pipeline...")
    await cognee.cognify()

    # 4. Ask natural language personal finance questions
    query = "How much did I spend on groceries and restaurants this month, and which categories are over budget?"
    print(f"\nSearching Cognee memory: '{query}'")
    results = await cognee.search(query)
    for result in results:
        print(f"- {result}")


if __name__ == "__main__":
    asyncio.run(main())
