"""Runnable example for ingesting Strapi CMS content into Cognee."""

import asyncio
import os

import cognee
from cognee_community_connector_strapi import strapi_source


async def main() -> None:
    api_token = os.getenv("STRAPI_API_TOKEN", "strapi_dummy_token")
    base_url = os.getenv("STRAPI_BASE_URL", "http://localhost:1337")

    print("Initializing Strapi connector...")
    source = strapi_source(
        content_types=["articles", "documentation"],
        api_token=api_token,
        base_url=base_url,
        incremental=False,
    )

    print("Adding Strapi source to Cognee...")
    await cognee.add(source)

    print("Building knowledge graph with cognify...")
    await cognee.cognify()

    print("Searching indexed CMS knowledge...")
    results = await cognee.search("What is the latest product update in the documentation?")
    print(f"Search results: {results}")


if __name__ == "__main__":
    asyncio.run(main())
