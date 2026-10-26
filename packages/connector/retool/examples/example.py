import asyncio
import os
import cognee
from cognee_community_connector_retool import retool_source

async def main():
    token = os.environ.get("RETOOL_API_KEY", "mock_retool_token")

    print("Connecting Retool source into Cognee...")
    source = retool_source(
        token=token,
        resources=["apps", "workflows", "resources"],
    )

    await cognee.prune.prune_data()
    await cognee.remember(
        source,
        dataset_name="retool_metadata",
        primary_key="id",
        write_disposition="merge",
        max_rows_per_table=0,
    )
    print("Retool apps, workflows, and resources indexed into Cognee successfully!")

if __name__ == "__main__":
    asyncio.run(main())
