"""Runnable example for ingesting Ashby recruiting jobs into Cognee."""

import asyncio
import os

import cognee
from cognee_community_connector_ashby import ashby_source


async def main() -> None:
    api_key = os.getenv("ASHBY_API_KEY", "ashby_dummy_key")

    print("Initializing Ashby connector...")
    source = ashby_source(
        api_key=api_key,
        status_filter="Open",
        include_job_postings=True,
        incremental=False,
    )

    print("Adding Ashby source to Cognee...")
    await cognee.add(source)

    print("Building knowledge graph with cognify...")
    await cognee.cognify()

    print("Searching indexed recruiting knowledge...")
    results = await cognee.search("What are the requirements for the Senior Infrastructure role?")
    print(f"Search results: {results}")


if __name__ == "__main__":
    asyncio.run(main())
