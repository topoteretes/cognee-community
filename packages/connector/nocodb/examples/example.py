"""Runnable example for ingesting NocoDB database table records into Cognee."""

import asyncio
import os

import cognee
from cognee_community_connector_nocodb import nocodb_source


async def main() -> None:
    api_token = os.getenv("NOCODB_API_TOKEN", "nocodb_dummy_token")
    base_url = os.getenv("NOCODB_BASE_URL", "http://localhost:8080")
    table_id = os.getenv("NOCODB_TABLE_ID", "m123456789")

    print("Initializing NocoDB connector...")
    source = nocodb_source(
        table_ids=[table_id],
        api_token=api_token,
        base_url=base_url,
        incremental=False,
    )

    print("Adding NocoDB source to Cognee...")
    await cognee.add(source)

    print("Building knowledge graph with cognify...")
    await cognee.cognify()

    print("Searching indexed relational table knowledge...")
    results = await cognee.search("Which hardware vendors handle overseas shipping?")
    print(f"Search results: {results}")


if __name__ == "__main__":
    asyncio.run(main())
