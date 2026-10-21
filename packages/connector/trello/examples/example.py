"""Trello connector demo: turn your Trello boards into memory.

``trello_source`` returns a ``dlt`` source you hand straight to ``cognee.remember``.
Every card (list, labels, members, dates, description, checklists, comments) and
every board is ingested as a normal document, so it goes through cognify entity
extraction.

The script syncs twice: change something on the board in between (edit or
comment on a card, delete one) and the second sync processes only that change.

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


async def sync(board_id: str) -> None:
    source = trello_source(
        board_ids=[board_id],
        api_key=os.environ["TRELLO_API_KEY"],
        token=os.environ["TRELLO_TOKEN"],
    )
    await cognee.remember(
        source,
        dataset_name=DATASET_NAME,
        primary_key="id",
        write_disposition="merge",
        max_rows_per_table=0,
        self_improvement=False,
    )
    print("Sync stats:", source.cognee_sync_stats)


async def main() -> None:
    required = ("TRELLO_API_KEY", "TRELLO_TOKEN", "TRELLO_BOARD_ID")
    if not all(os.environ.get(name) for name in required):
        print(f"Set {', '.join(required)} to run this example.")
        return
    board_id = os.environ["TRELLO_BOARD_ID"]

    print("First sync: every card and the board are ingested ...")
    await sync(board_id)
    results = await cognee.recall(
        "What is in progress on this board and who is working on it?", datasets=[DATASET_NAME]
    )
    print("\nRecall result:\n", results)

    await asyncio.to_thread(
        input, "\nEdit, comment on or delete a card in Trello, then press Enter to sync again."
    )
    print("Second sync: only what changed is processed ...")
    await sync(board_id)


if __name__ == "__main__":
    asyncio.run(main())
