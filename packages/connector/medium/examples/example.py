"""Runnable example of ingesting Medium articles into Cognee."""

import asyncio
import cognee
from cognee_community_connector_medium import medium_source


async def main():
    # Ingest from author handle or publication feed
    source = medium_source(
        authors=["@towardsdatascience"],
        article_urls=[
            "https://medium.com/@author/building-ai-agents-with-memory-12345",
        ],
    )

    print("Ingesting Medium articles into Cognee memory...")
    await cognee.add(source)

    print("Cognifying graph knowledge...")
    await cognee.cognify()

    print("Querying ingested Medium knowledge...")
    results = await cognee.search("What are best practices for memory-enabled AI agents?")
    for res in results:
        print("-", res)


if __name__ == "__main__":
    asyncio.run(main())
