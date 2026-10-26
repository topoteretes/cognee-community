"""Example script syncing Tailscale mesh network topology and ACLs into Cognee."""

import asyncio
import os
import cognee
from cognee_community_connector_tailscale import tailscale_source


async def main():
    api_key = os.environ.get("TAILSCALE_API_KEY", "mock_key")
    tailnet = os.environ.get("TAILSCALE_TAILNET", "-")

    # Ingest devices, ACL access policies, and subnet routes
    source = tailscale_source(
        api_key=api_key,
        tailnet=tailnet,
        resources=["devices", "acls", "routes"],
    )

    await cognee.remember(
        source,
        dataset_name="tailscale_network",
        primary_key="id",
        write_disposition="merge",
        max_rows_per_table=0,
    )

    # Query Cognee memory
    results = await cognee.search("Which devices are advertising subnet routes in the tailnet?")
    for res in results:
        print(res)


if __name__ == "__main__":
    asyncio.run(main())
