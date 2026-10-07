"""Ingest selected Asana projects into cognee and search them."""

import asyncio
import os

import cognee

from cognee_community_connector_asana import asana_source

DATASET_NAME = "asana"


async def main() -> None:
    project_ids = [item.strip() for item in os.environ.get("ASANA_PROJECT_IDS", "").split(",")]
    project_ids = [item for item in project_ids if item]
    if not os.environ.get("ASANA_ACCESS_TOKEN") or not project_ids:
        print("Set ASANA_ACCESS_TOKEN and comma-separated ASANA_PROJECT_IDS to run this example.")
        return

    await cognee.remember(
        asana_source(project_ids=project_ids),
        dataset_name=DATASET_NAME,
        primary_key="id",
        write_disposition="merge",
        max_rows_per_table=0,
    )

    result = await cognee.search(
        query_text="What work is currently planned in Asana, and what context is in the comments?",
        query_type=cognee.SearchType.GRAPH_COMPLETION,
        datasets=[DATASET_NAME],
    )
    print(result)


if __name__ == "__main__":
    asyncio.run(main())
