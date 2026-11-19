"""Runnable example demonstrating Knock ingestion into Cognee."""

import asyncio
import os
import sys

import cognee

from cognee_community_connector_knock import knock_source


async def main() -> None:
    api_key = os.environ.get("KNOCK_API_KEY")
    if not api_key:
        print("KNOCK_API_KEY environment variable not set.")
        print("Please set it to run against live Knock API.")
        sys.exit(0)

    print("Configuring Knock connector...")
    source = knock_source(api_key=api_key, include=("workflows", "channels"))

    print("Adding Knock documents to Cognee...")
    await cognee.add(source)

    print("Running Cognify pipeline...")
    await cognee.cognify()

    print("Searching for notification channels...")
    results = await cognee.recall("What delivery channels are configured in Knock?")
    for res in results:
        print(f"- {res}")


if __name__ == "__main__":
    asyncio.run(main())
