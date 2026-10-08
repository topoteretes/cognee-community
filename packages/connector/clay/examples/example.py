"""Example script demonstrating how to ingest a Clay table into cognee."""

import asyncio
import os

import cognee

from cognee_community_connector_clay import clay_source


async def main() -> None:
    # 1. Retrieve credentials from environment variables or provide explicitly
    table_id = os.getenv("CLAY_TABLE_ID", "t_0te9i4tZEHwc9hihBXu")
    api_key = os.getenv("CLAY_API_KEY", "your_clay_api_key")

    # 2. Select only first-party customer columns to comply with licensing agreements
    selected_fields = ["Company Name", "Domain", "Account Owner"]

    # 3. Create the dlt resource with snapshot replace disposition
    resource = clay_source(
        table_id=table_id,
        api_key=api_key,
        fields=selected_fields,
        primary_key="domain",
        write_disposition="replace",
    )

    # 4. Ingest into cognee memory
    print(f"Ingesting Clay table '{table_id}' into cognee memory...")
    await cognee.remember(
        resource,
        dataset_name="clay_accounts",
    )
    print("Ingestion complete! Data is now available in cognee.")


if __name__ == "__main__":
    asyncio.run(main())
