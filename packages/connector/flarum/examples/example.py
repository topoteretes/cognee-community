"""Example demonstrating how to ingest Flarum forum discussions into Cognee."""

import asyncio
import os
import cognee
from cognee_community_connector_flarum import flarum_source


async def main() -> None:
    # 1. Initialize Flarum source
    source = flarum_source(
        base_url=os.getenv("FLARUM_BASE_URL", "https://discuss.flarum.org"),
        token=os.getenv("FLARUM_TOKEN"),  # Optional, works as guest for public forums
        tags=["general", "feedback"],
        limit=20,
    )

    # 2. Add to Cognee memory
    print("Ingesting Flarum forum discussions into Cognee...")
    await cognee.add(source)

    # 3. Cognify into knowledge graph
    print("Running Cognify extraction pipeline...")
    await cognee.cognify()

    # 4. Search community discussions
    query = "What feedback or questions have users posted regarding extension installations?"
    print(f"\nSearching Cognee memory: '{query}'")
    results = await cognee.search(query)
    for result in results:
        print(f"- {result}")


if __name__ == "__main__":
    asyncio.run(main())
