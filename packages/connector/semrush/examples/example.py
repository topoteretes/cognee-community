"""Semrush connector demo — sync domain rankings and organic keywords into memory.

Pull Semrush domain overview (rankings, traffic, search volume) and organic search
keywords into cognee. ``semrush_source`` returns a ``dlt`` source that routes directly
into ``cognee.remember``.

Cost Warning:
Requests specify a historical monthly period (YYYYMM15) billed at Semrush's
historical rate of 50 API units per line. For 2 domains and display_limit=5,
each fetch costs:
    2 domains * (1 + 5) lines * 50 units = 600 API units.
Subsequent runs with unchanged parameters trigger the skip gate, costing 0 API units.

────────────────────────────────────────────────────────────────────────────
One-time setup
────────────────────────────────────────────────────────────────────────────
1. Export your Semrush API key and LLM API key (required by cognee.search):

       export SEMRUSH_API_KEY="your_api_key_here"
       export LLM_API_KEY="sk-..."

2. Run the example:

       uv run python examples/example.py
"""

import asyncio
import os

import cognee

from cognee_community_connector_semrush import semrush_source

DATASET_NAME = "semrush"


async def main() -> None:
    if not os.environ.get("SEMRUSH_API_KEY"):
        raise SystemExit("Set SEMRUSH_API_KEY to run this example.")

    # Configure Semrush source for target domains and regional database.
    # display_limit=5 bounds API unit consumption to 600 units per initial fetch.
    source = semrush_source(
        domains=["github.com", "python.org"],
        database="us",
        display_limit=5,
    )

    print(f"Syncing Semrush domain intelligence into dataset '{DATASET_NAME}' ...")
    await cognee.remember(source, dataset_name=DATASET_NAME)
    print("Sync complete!")

    answer = await cognee.search(
        query_text="What is the organic search ranking and traffic for github.com?",
        query_type=cognee.SearchType.GRAPH_COMPLETION,
        datasets=[DATASET_NAME],
    )
    print("\nSearch result:\n", answer)


if __name__ == "__main__":
    asyncio.run(main())
