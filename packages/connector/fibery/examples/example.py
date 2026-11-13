import asyncio
import os

import cognee

from cognee_community_connector_fibery import fibery_source


async def main() -> None:
    databases = [name.strip() for name in os.environ["FIBERY_DATABASES"].split(",") if name.strip()]
    await cognee.remember(
        fibery_source(os.environ["FIBERY_ACCOUNT"], databases),
        dataset_name="fibery",
        primary_key="id",
        write_disposition="merge",
        max_rows_per_table=0,
    )
    answer = await cognee.search(
        query_text="What did we decide about billing, and which record owns it?",
        datasets=["fibery"],
    )
    print(answer)


if __name__ == "__main__":
    asyncio.run(main())
