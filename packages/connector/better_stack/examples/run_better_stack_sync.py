"""Example demonstrating how to sync Better Stack incidents into Cognee memory."""

import asyncio
import os
import cognee
from cognee_community_connector_better_stack import better_stack_source


async def main():
    api_token = os.environ.get("BETTER_STACK_API_TOKEN", "mock_token")
    print("Connecting to Better Stack and retrieving outage incidents...")

    # Build the Better Stack dlt source
    source = better_stack_source(api_token=api_token)

    # Ingest outage reports into Cognee
    print("Ingesting incident records into Cognee memory...")
    await cognee.add(source)

    # Cognify extracts entities and constructs the system outage graph
    print("Cognifying infrastructure observability graph...")
    await cognee.cognify()

    # Search past outage history
    query = "Which endpoints suffered downtime due to 502 Bad Gateway?"
    print(f"\nSearching Cognee memory: '{query}'")
    results = await cognee.search(query)
    print("Results:")
    for res in results:
        print(f"- {res}")


if __name__ == "__main__":
    asyncio.run(main())
