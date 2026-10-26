"""Example script syncing Rippling workforce directory and policies into Cognee."""

import asyncio
import os
import cognee
from cognee_community_connector_rippling import rippling_source


async def main():
    token = os.environ.get("RIPPLING_API_TOKEN", "mock_token")

    # Ingest employees, organizational departments, and company policies
    source = rippling_source(
        api_token=token,
        resources=["employees", "departments", "policies"],
    )

    await cognee.remember(
        source,
        dataset_name="rippling_workforce",
        primary_key="id",
        write_disposition="merge",
        max_rows_per_table=0,
    )

    # Query Cognee memory
    results = await cognee.search("Who are the leads in the Infrastructure Engineering department?")
    for res in results:
        print(res)


if __name__ == "__main__":
    asyncio.run(main())
