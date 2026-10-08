"""Example: sync Raindrop.io bookmarks into cognee.

Usage:
    export RAINDROP_TOKEN="your-test-token-here"
    python example.py

Get a test token at: https://app.raindrop.io/#settings/apps
"""

import asyncio

import cognee
from cognee_community_connector_raindrop import raindrop_source


async def main():
    # Add Raindrop.io bookmarks via the connector.
    # Token is read from RAINDROP_TOKEN env var when not passed explicitly.
    source = raindrop_source()

    # Alternative: pass token directly or restrict to specific collections
    # source = raindrop_source(token="...", collection_ids=[123456, 789012])

    await cognee.add(source, max_rows_per_table=0)

    # Query your synced bookmarks
    results = await cognee.search(query_text="machine learning")
    for result in results:
        print(result)


if __name__ == "__main__":
    asyncio.run(main())
