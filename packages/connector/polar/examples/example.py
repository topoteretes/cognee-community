"""Polar connector example — index Polar.sh customers, subscriptions, and benefit grants into memory.

Usage:
    export POLAR_ACCESS_TOKEN="polar_oat_..."
    export POLAR_ORGANIZATION_ID="org_..."
    export POLAR_SANDBOX="false"  # or "true" for sandbox
    python examples/example.py
"""

import asyncio
import os
from cognee_community_connector_polar.polar import polar_source


async def main():
    token = os.getenv("POLAR_ACCESS_TOKEN", "mock_token")
    org_id = os.getenv("POLAR_ORGANIZATION_ID", "mock_org")
    sandbox = os.getenv("POLAR_SANDBOX", "false").lower() in ("true", "1")

    print(f"Connecting to Polar (Sandbox: {sandbox})...")
    source = polar_source(access_token=token, organization_id=org_id, sandbox=sandbox)

    # In cognee environment:
    # import cognee
    # await cognee.remember(source, dataset_name="polar", primary_key="id", write_disposition="merge")
    # results = await cognee.search("Which customers have active Pro subscriptions?")
    print("Source initialized successfully.")


if __name__ == "__main__":
    asyncio.run(main())
