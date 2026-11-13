"""Example demonstrating PocketBase ingestion into Cognee."""

import asyncio
import os

import cognee

from cognee_community_connector_pocketbase import pocketbase_source


async def main():
    pocketbase_url = os.environ.get("POCKETBASE_URL", "http://127.0.0.1:8090")
    pocketbase_token = os.environ.get("POCKETBASE_TOKEN", None)

    print(f"Connecting to PocketBase instance at {pocketbase_url}...")

    # Configure PocketBase data source
    source = pocketbase_source(
        base_url=pocketbase_url,
        auth_token=pocketbase_token,
        collections=["articles", "notes"],
    )

    # Ingest records into Cognee
    print("Ingesting records into Cognee...")
    await cognee.add(source)

    # Cognify records into knowledge graph and vector representations
    print("Cognifying ingested memory...")
    await cognee.cognify()

    # Query Cognee memory
    query = "What are the key points in the latest notes?"
    print(f"Searching Cognee memory: '{query}'")
    results = await cognee.search(query)
    print("Results:", results)


if __name__ == "__main__":
    asyncio.run(main())
