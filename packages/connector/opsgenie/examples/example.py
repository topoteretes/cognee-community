"""Example usage of the Cognee Opsgenie connector."""

import asyncio
import os

import cognee

from cognee_community_connector_opsgenie import opsgenie_source


async def main():
    api_key = os.environ.get("OPSGENIE_API_KEY", "mock-genie-key")

    source = opsgenie_source(
        api_key=api_key,
        query="status: open",
    )

    print("Adding Opsgenie alerts and on-call schedules to Cognee...")
    await cognee.add(source)

    print("Running Cognify on incident graph...")
    await cognee.cognify()

    print("Searching for on-call responders...")
    results = await cognee.search("Which teams are assigned to open P1 incidents?")
    for res in results:
        print("-", res)


if __name__ == "__main__":
    asyncio.run(main())
