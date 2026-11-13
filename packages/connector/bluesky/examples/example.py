"""Example demonstrating Bluesky connector ingestion into Cognee memory."""

import asyncio

import cognee

from cognee_community_connector_bluesky import bluesky_source


async def main() -> None:
    # Reset existing memory for demo
    await cognee.prune.prune_data()
    await cognee.prune.prune_system(metadata=True)

    # Ingest posts from official Bluesky account or keyword search
    source = bluesky_source(
        actors=["bsky.app"],
        queries=["GraphRAG"],
        limit_per_feed=20,
    )

    await cognee.add(source)
    await cognee.cognify()

    # Query the knowledge graph about discussions
    search_results = await cognee.search(
        "What recent announcements or technical discussions were shared on Bluesky?"
    )

    print("Search results from Cognee:")
    for result in search_results:
        print(f"- {result}")


if __name__ == "__main__":
    asyncio.run(main())
