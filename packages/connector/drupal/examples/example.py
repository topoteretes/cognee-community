"""Example demonstrating how to ingest Drupal CMS documentation into Cognee."""

import asyncio
import os
import cognee
from cognee_community_connector_drupal import drupal_source


async def main() -> None:
    # 1. Initialize Drupal source
    source = drupal_source(
        base_url=os.getenv("DRUPAL_BASE_URL", "https://cms.internal.example.org"),
        bundles=["article", "page"],
        username=os.getenv("DRUPAL_USERNAME"),
        password=os.getenv("DRUPAL_PASSWORD"),
        limit=25,
    )

    # 2. Add to Cognee memory
    print("Ingesting Drupal CMS articles and pages into Cognee...")
    await cognee.add(source)

    # 3. Cognify into knowledge graph
    print("Running Cognify extraction pipeline...")
    await cognee.cognify()

    # 4. Search company knowledge
    query = "What is the employee remote work policy and how do we request equipment?"
    print(f"\nSearching Cognee memory: '{query}'")
    results = await cognee.search(query)
    for result in results:
        print(f"- {result}")


if __name__ == "__main__":
    asyncio.run(main())
