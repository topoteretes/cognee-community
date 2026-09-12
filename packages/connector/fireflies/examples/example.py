"""Runnable Fireflies -> cognee ingestion and search example."""

import asyncio
import os

import cognee

from cognee_community_connector_fireflies import fireflies_source

DATASET_NAME = "fireflies_meetings"


async def main():
    if not os.environ.get("FIREFLIES_API_KEY"):
        print("Set FIREFLIES_API_KEY before running this example.")
        return

    source = fireflies_source(
        include_transcript=True,
        include_summary=True,
        include_action_items=True,
        include_speakers=True,
    )
    await cognee.remember(
        source,
        dataset_name=DATASET_NAME,
        primary_key="id",
        write_disposition="merge",
        max_rows_per_table=0,
    )
    await cognee.cognify(dataset_name=DATASET_NAME)

    results = await cognee.search(
        query_text="Who owns the action items from my recent meetings?",
        query_type=cognee.SearchType.GRAPH_COMPLETION,
        datasets=[DATASET_NAME],
    )
    print(results)


if __name__ == "__main__":
    asyncio.run(main())
