"""Example demonstrating Directus collections ingestion into Cognee."""

import asyncio
import os

import cognee

from cognee_community_connector_directus import directus_source


async def main():
    directus_url = os.environ.get("DIRECTUS_URL", "http://127.0.0.1:8055")
    directus_token = os.environ.get("DIRECTUS_TOKEN", None)

    print(f"Connecting to Directus instance at {directus_url}...")

    # Configure Directus data source
    source = directus_source(
        base_url=directus_url,
        auth_token=directus_token,
        collections=["articles", "documentation"],
    )

    # Ingest items into Cognee
    print("Ingesting items into Cognee...")
    await cognee.add(source)

    # Cognify items into knowledge graph and vector representations
    print("Cognifying ingested memory...")
    await cognee.cognify()

    # Query Cognee memory
    query = "What are the latest updates in documentation?"
    print(f"Searching Cognee memory: '{query}'")
    results = await cognee.search(query)
    print("Results:", results)


if __name__ == "__main__":
    asyncio.run(main())
