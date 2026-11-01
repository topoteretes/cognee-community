"""Runnable example for ingesting Cal.com bookings into Cognee."""

import asyncio
import os

import cognee
from cognee_community_connector_calcom import calcom_source


async def main() -> None:
    api_key = os.getenv("CALCOM_API_KEY", "cal_test_dummy_key")

    print("Initializing Cal.com connector...")
    source = calcom_source(
        api_key=api_key,
        status_filter="ACCEPTED",
        incremental=False,
    )

    print("Adding Cal.com source to Cognee...")
    await cognee.add(source)

    print("Building knowledge graph with cognify...")
    await cognee.cognify()

    print("Searching indexed meeting memory...")
    results = await cognee.search("What meetings are scheduled regarding architecture?")
    print(f"Search results: {results}")


if __name__ == "__main__":
    asyncio.run(main())
