"""Runnable Fireflies -> cognee ingestion and search example."""

import asyncio
import os

import cognee

from cognee_community_connector_fireflies import fireflies_source, sync_fireflies

DATASET_NAME = os.environ.get("FIREFLIES_DATASET_NAME", "fireflies_meetings")


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
    result = await sync_fireflies(source, dataset_name=DATASET_NAME)
    if result is None:
        print(
            "Fireflies returned no visible meetings. Create a test meeting in the "
            "account that owns this API key, wait for transcription, then run again. "
            "Search was skipped because the Fireflies dataset is empty."
        )
        return

    results = await cognee.search(
        query_text="Who owns the action items from my recent meetings?",
        query_type=cognee.SearchType.GRAPH_COMPLETION,
        datasets=[DATASET_NAME],
    )
    print(results)


if __name__ == "__main__":
    asyncio.run(main())
