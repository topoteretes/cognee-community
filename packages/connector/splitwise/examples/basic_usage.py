"""Splitwise connector demo — sync shared expenses into memory.

Pull Splitwise expenses, groups, and balances into cognee with incremental
sync and merge-mode deletion handling. ``splitwise_source`` returns a ``dlt``
source that you pass directly to ``cognee.remember`` — no manual routing needed.

Expenses are ingested as normal documents (so they flow through the complete
cognify entity-extraction and knowledge graph pipeline).

Sync & Deletion modes:
- Expenses: merge mode with hard-delete tombstones (voided expenses emit
  ``_deleted=True``).
- Groups & Balances: merge mode (upsert by ID).

Incremental sync utilizes an ``updated_at`` watermark in dlt state so only
new/changed expenses are fetched on re-sync.

────────────────────────────────────────────────────────────────────────────
One-time setup
────────────────────────────────────────────────────────────────────────────
1. Install the connector:

       cd packages/connector/splitwise && uv sync

2. Obtain a Splitwise personal access token from
   https://secure.splitwise.com/oauth/tokens (or use OAuth1 flow).
3. Export your key and run:

       export SPLITWISE_API_KEY="your_token_here"
       export LLM_API_KEY="sk-..."
       uv run python examples/basic_usage.py
"""

import asyncio
import os

import cognee

from cognee_community_connector_splitwise import splitwise_source

DATASET_NAME = "splitwise"


async def main() -> None:
    if not os.environ.get("SPLITWISE_API_KEY"):
        print("Please set SPLITWISE_API_KEY in your environment to run this example.")
        return

    # Sync all expenses (or filter by group_ids=[...])
    print("Syncing Splitwise expenses into cognee ...")
    source = splitwise_source(
        # group_ids=[12345],  # Optional: filter to specific group(s)
        include_groups=True,
        include_balances=True,
        max_expenses=1000,
    )

    await cognee.remember(source, dataset_name=DATASET_NAME)

    print("\nQuerying cognee knowledge graph for shared expenses ...")
    answer = await cognee.search(
        query_text="Who still owes me money across all groups?",
        query_type=cognee.SearchType.GRAPH_COMPLETION,
        datasets=[DATASET_NAME],
    )
    print("\nSearch result:\n", answer)

    # Trip-specific query
    answer2 = await cognee.search(
        query_text="What did we spend on the Goa trip and how was it split?",
        query_type=cognee.SearchType.GRAPH_COMPLETION,
        datasets=[DATASET_NAME],
    )
    print("\nTrip query result:\n", answer2)


if __name__ == "__main__":
    asyncio.run(main())
