"""Example script syncing Vanta compliance controls and policies into Cognee."""

import asyncio
import os
import cognee
from cognee_community_connector_vanta import vanta_source


async def main():
    token = os.environ.get("VANTA_API_TOKEN", "mock_token")

    # Ingest controls, policies, and vendor risk inventory
    source = vanta_source(
        api_token=token,
        resources=["controls", "policies", "vendors"],
    )

    await cognee.remember(
        source,
        dataset_name="vanta_compliance",
        primary_key="id",
        write_disposition="merge",
        max_rows_per_table=0,
    )

    # Query Cognee memory
    results = await cognee.search("Which vendors have high risk ratings and need security review?")
    for res in results:
        print(res)


if __name__ == "__main__":
    asyncio.run(main())
