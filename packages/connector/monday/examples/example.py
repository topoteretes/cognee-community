"""Sync monday.com boards into cognee memory, incrementally.

Items from the selected boards, with their sub-items, descriptions and updates,
are turned into documents and cognified. Re-running this script only processes
what changed since the last run, and archived or deleted items are forgotten.

Setup (see the README for details):
  1. In monday.com: avatar > Developers > API token, and copy it.
  2. Set:
       MONDAY_API_TOKEN=...
       MONDAY_BOARD_IDS=1234567890        (comma-separated; see list_boards.py)
     plus the usual cognee LLM settings, e.g. LLM_API_KEY.

Run:
    python examples/example.py ["optional question about your boards"]
    python examples/example.py --full-refresh   (also picks up edited descriptions)
"""

import asyncio
import sys

import cognee

from cognee_community_connector_monday import monday_source

DATASET = "monday_demo"


async def main():
    args = [arg for arg in sys.argv[1:] if arg != "--full-refresh"]
    # Reads MONDAY_API_TOKEN and MONDAY_BOARD_IDS.
    source = monday_source(full_refresh="--full-refresh" in sys.argv)

    print("=== Sync ===")
    await cognee.remember(
        source,
        dataset_name=DATASET,
        primary_key="id",
        # "merge" is required: it makes re-runs incremental and makes
        # deletions propagate through cognee's orphan cleanup.
        write_disposition="merge",
        # The default caps a table at 50 rows; a board easily exceeds it.
        max_rows_per_table=0,
    )
    # Counts only, never names or content.
    print("Sync stats:", source.cognee_sync_stats)

    question = args[0] if args else "What is stuck right now, and who owns it?"
    answer = await cognee.recall(question, datasets=[DATASET])
    print("Recall:", answer)


if __name__ == "__main__":
    asyncio.run(main())
