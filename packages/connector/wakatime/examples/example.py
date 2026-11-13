"""Runnable example of ingesting WakaTime developer activity into Cognee."""

import asyncio
import os
import cognee
from cognee_community_connector_wakatime import wakatime_source


async def main():
    # Ingest last 7 days of developer coding activity
    source = wakatime_source(
        api_key=os.environ.get("WAKATIME_API_KEY", "your-wakatime-api-key"),
        days=7,
    )

    print("Adding WakaTime activity source to Cognee memory...")
    await cognee.add(source)

    print("Cognifying productivity graph...")
    await cognee.cognify()

    print("Querying developer activity knowledge...")
    answers = await cognee.search("Which project took the most time last week and what languages were used?")
    for item in answers:
        print("-", item)


if __name__ == "__main__":
    asyncio.run(main())
