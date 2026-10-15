"""Sync selected Grain content into cognee and search it.

Run from ``packages/connector/grain`` after setting ``GRAIN_API_TOKEN`` and the
LLM credentials required by your cognee provider::

    uv run python examples/example.py
"""

import asyncio
import os

import cognee

from cognee_community_connector_grain import grain_source

DATASET_NAME = "grain"


async def main() -> None:
    if not os.environ.get("GRAIN_API_TOKEN"):
        print("Set GRAIN_API_TOKEN before running this example.")
        return

    source = grain_source(
        # Choose any non-empty combination. Curated highlights are indexed as
        # standalone high-priority documents; transcripts remain searchable too.
        content_types=["recordings", "transcripts", "highlights"],
    )
    await cognee.remember(
        source,
        dataset_name=DATASET_NAME,
        primary_key="id",
        write_disposition="merge",
    )

    answer = await cognee.search(
        query_text="Summarize the important decisions in my recent Grain meetings.",
        query_type=cognee.SearchType.GRAPH_COMPLETION,
        datasets=[DATASET_NAME],
    )
    print(answer)


if __name__ == "__main__":
    asyncio.run(main())
