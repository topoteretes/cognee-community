"""Example demonstrating Ghost CMS publication ingestion into Cognee."""

import asyncio
import os

import cognee

from cognee_community_connector_ghost import ghost_source


async def main():
    ghost_url = os.environ.get("GHOST_URL", "https://demo.ghost.io")
    ghost_key = os.environ.get("GHOST_CONTENT_API_KEY", "22444f484471c222c61b03ad88")

    print(f"Connecting to Ghost publication at {ghost_url}...")

    # Configure Ghost data source
    source = ghost_source(
        base_url=ghost_url,
        content_api_key=ghost_key,
        include_posts=True,
        include_pages=True,
    )

    # Ingest posts and pages into Cognee
    print("Ingesting publication documents into Cognee...")
    await cognee.add(source)

    # Cognify items into knowledge graph and vector representations
    print("Cognifying ingested memory...")
    await cognee.cognify()

    # Query Cognee memory
    query = "What articles discuss our latest architecture changes?"
    print(f"Searching Cognee memory: '{query}'")
    results = await cognee.search(query)
    print("Results:", results)


if __name__ == "__main__":
    asyncio.run(main())
