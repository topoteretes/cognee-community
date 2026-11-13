"""Sync selected Fibery databases and search their contents with Cognee."""

import asyncio
import os

import cognee

from cognee_community_connector_fibery import fibery_source


async def main() -> None:
    account = os.environ.get("FIBERY_ACCOUNT")
    databases = [name.strip() for name in os.environ.get("FIBERY_DATABASES", "Product/Feature").split(",")]
    if not account or not os.environ.get("FIBERY_API_TOKEN"):
        print("Set FIBERY_ACCOUNT and FIBERY_API_TOKEN to run this example.")
        return

    await cognee.remember(
        fibery_source(account=account, databases=databases),
        dataset_name="fibery",
        primary_key="id",
        write_disposition="merge",
        max_rows_per_table=0,
    )
    answer = await cognee.search(
        query_text="Summarize the Fibery entities in this workspace.",
        query_type=cognee.SearchType.GRAPH_COMPLETION,
        datasets=["fibery"],
    )
    print(answer)


if __name__ == "__main__":
    asyncio.run(main())
