"""Runnable example for ingesting Loom video transcripts into Cognee."""

import asyncio
import os

import cognee
from cognee_community_connector_loom import loom_source


async def main() -> None:
    api_token = os.getenv("LOOM_API_TOKEN", "loom_dummy_token")

    print("Initializing Loom connector...")
    source = loom_source(
        api_token=api_token,
        fetch_transcripts=True,
        incremental=False,
    )

    print("Adding Loom source to Cognee...")
    await cognee.add(source)

    print("Building knowledge graph with cognify...")
    await cognee.cognify()

    print("Searching indexed video transcripts...")
    results = await cognee.search("What technical demo was presented regarding the API?")
    print(f"Search results: {results}")


if __name__ == "__main__":
    asyncio.run(main())
