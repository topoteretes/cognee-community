"""Trello connector demo: turn your Trello boards into memory.

``trello_source`` returns a ``dlt`` source you hand straight to ``cognee.remember``.
Every card (list, labels, members, dates, description, checklists, comments) and
every board is ingested as a normal document, so it goes through cognify entity
extraction.

Re-running syncs only what changed: edited cards are processed again and cards
or boards you delete are forgotten.

Privacy / opt-in
----------------
This reads your boards, including comments. Nothing is fetched until you run
this script. Use a read-only token and keep Trello in its own dataset.

One-time setup
--------------
1. Generate an API key for a Power-Up at https://trello.com/apps/admin and a
   read-only token (see the README).
2. Export them, the board to sync and your LLM key, then run:

       export TRELLO_API_KEY="..."
       export TRELLO_TOKEN="..."
       export TRELLO_BOARD_ID="..."
       export LLM_API_KEY="sk-..."
       uv run python examples/example.py
"""

import asyncio
import os

import cognee

from cognee_community_connector_trello import trello_source

DATASET_NAME = "trello"


async def main() -> None:
    required = ("TRELLO_API_KEY", "TRELLO_TOKEN", "TRELLO_BOARD_ID")
    if not all(os.environ.get(name) for name in required):
        print(f"Set {', '.join(required)} to run this example.")
        return

    source = trello_source(board_ids=[os.environ["TRELLO_BOARD_ID"]])

    print("Syncing Trello into cognee ...")
    await cognee.remember(
        source,
        dataset_name=DATASET_NAME,
        primary_key="id",
        write_disposition="merge",
        max_rows_per_table=0,
        self_improvement=False,
    )
    print("Sync stats:", source.cognee_sync_stats)

    results = await cognee.recall(
        "What is in progress on this board and who is working on it?", datasets=[DATASET_NAME]
    )
    print("\nRecall result:\n", results)

    print(
        "\nEdit, comment on or delete a card, then re-run: only the changes are synced "
        "and deleted cards are forgotten."
    )


if __name__ == "__main__":
    asyncio.run(main())
