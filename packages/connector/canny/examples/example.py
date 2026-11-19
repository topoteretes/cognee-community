"""Canny connector demo - turn your feature requests into memory.

Pull Canny posts (with vote counts, status and comments) into cognee, with
forget-on-delete. ``canny_source`` returns a ``dlt`` resource you hand to
``cognee.remember``. Each post becomes a normal document, so it goes through the
full cognify entity-extraction pipeline.

Each run is a full snapshot: unchanged posts are not re-cognified, and posts you
delete in Canny drop out of the snapshot, so cognee forgets them on the next run.

Privacy: this reads your feedback board. Nothing is fetched until you run it.
Limit it with ``board_ids=[...]`` / ``statuses=[...]`` and use a dedicated dataset.

Setup:

    cd packages/connector/canny && uv sync --all-extras
    # secret API key: Canny company settings -> API
    export CANNY_API_KEY="..."
    export LLM_API_KEY="sk-..."
    uv run python examples/example.py
"""

import asyncio
import os

import cognee

from cognee_community_connector_canny import canny_source

DATASET_NAME = "canny"


async def main() -> None:
    if not os.environ.get("CANNY_API_KEY"):
        print("Set CANNY_API_KEY (Canny company settings -> API) to run this example.")
        return

    # Limit with board_ids=[...] or statuses=["planned", "in progress"].
    source = canny_source()

    print("Syncing Canny posts into cognee ...")
    # Keep the default write_disposition ("replace"): each run is a full snapshot.
    await cognee.remember(source, dataset_name=DATASET_NAME)

    answer = await cognee.search(
        query_text="Which feature requests have the most votes, and why do users want them?",
        query_type=cognee.SearchType.GRAPH_COMPLETION,
        datasets=[DATASET_NAME],
    )
    print("\nSearch result:\n", answer)


if __name__ == "__main__":
    asyncio.run(main())
