"""Example Calendly ingestion."""

import asyncio

import cognee

from cognee_community_connector_calendly import calendly_source


async def main():
    await cognee.remember(
        calendly_source(),
        dataset_name="calendly",
    )

    print("Calendly data ingested successfully.")


if __name__ == "__main__":
    asyncio.run(main())
