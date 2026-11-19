"""Example script demonstrating how to sync PagerDuty incidents into Cognee."""

import asyncio
import os
from cognee_community_connector_pagerduty import pagerduty_source

async def run_sync():
    api_token = os.environ.get("PAGERDUTY_API_TOKEN", "dummy_token")
    source = pagerduty_source(api_token=api_token, urgencies=["high"])

    try:
        import cognee
        print("Ingesting PagerDuty incidents into Cognee...")
        await cognee.add(source)
        await cognee.cognify()
        print("Sync complete. Searching graph...")
        results = await cognee.search("payment gateway incident")
        print("Search results:", results)
    except ImportError:
        print("Cognee is not installed. Extracted rows from source:")
        for resource in source.resources.values():
            for row in resource():
                print(f"- [{row['id']}] {row['title']}")

if __name__ == "__main__":
    asyncio.run(run_sync())
