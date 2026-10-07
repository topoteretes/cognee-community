"""Trello connector demo — turn your boards into cognee memory.

Sync Trello boards — cards, descriptions, comments, checklists, and board
structure. ``trello_source`` returns a ``dlt`` source you hand straight to
``cognee.remember``. Cards and board overviews are ingested as normal documents
(so they go through the full cognify entity-extraction pipeline).

Each run is incremental: the board's actions feed identifies the cards touched
since the last sync, and cards deleted or archived upstream are reconciled out
of memory (forget-on-delete).

────────────────────────────────────────────────────────────────────────────
Privacy / opt-in
────────────────────────────────────────────────────────────────────────────
This reads only the boards you list, with the read-only token you provide. It
is strictly opt-in — nothing is fetched until you run this script — and cards
go into a dedicated dataset so you can wipe it with a single
``cognee.forget(dataset)``.

────────────────────────────────────────────────────────────────────────────
One-time setup
────────────────────────────────────────────────────────────────────────────
1. Install the connector:

       cd packages/connector/trello && uv sync

2. Get an API key and generate a read token at
   https://developer.trello.com/power-ups/admin
3. Export your credentials and LLM key, then run:

       export TRELLO_API_KEY="..."
       export TRELLO_TOKEN="..."
       export LLM_API_KEY="sk-..."
       uv run python examples/example.py

Re-run after moving cards around to see the incremental sync; delete or
archive a card and re-run to see forget-on-delete.
"""

import asyncio
import os

import cognee

from cognee_community_connector_trello import trello_source

# Keep boards in their own dataset so they are easy to inspect and forget.
DATASET_NAME = "boards"

BOARD_IDS = [
    # Swap in your own board ids / short links (the part after /b/ in the URL).
    "YOUR_BOARD_ID",
]


async def main() -> None:
    if not os.environ.get("TRELLO_API_KEY") or not os.environ.get("TRELLO_TOKEN"):
        print("Set TRELLO_API_KEY and TRELLO_TOKEN (plus LLM_API_KEY) to run this example.")
        return
    if BOARD_IDS == ["YOUR_BOARD_ID"]:
        print("Edit BOARD_IDS in this example to list the boards you want to sync.")
        return

    print(f"Syncing {len(BOARD_IDS)} board(s) into cognee ...")
    await cognee.remember(
        trello_source(board_ids=BOARD_IDS),
        dataset_name=DATASET_NAME,
        primary_key="id",
        write_disposition="merge",
        max_rows_per_table=0,  # unlimited read-back so deletions reconcile fully
    )

    answer = await cognee.search(
        query_text="What is in progress and what is blocked on my boards?",
        query_type=cognee.SearchType.GRAPH_COMPLETION,
        datasets=[DATASET_NAME],
    )
    print("\nSearch result:\n", answer)

    print(
        "\nRe-run after card activity: only cards touched by board actions are "
        "re-ingested, and cards deleted or archived upstream are reconciled out "
        "of memory."
    )


if __name__ == "__main__":
    asyncio.run(main())
