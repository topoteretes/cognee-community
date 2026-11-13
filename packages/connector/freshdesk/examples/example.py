"""Sync a Freshdesk helpdesk into cognee memory, incrementally.

Tickets (with their replies) and published solution articles are turned into
documents and cognified.  Re-running this script only processes what changed
since the last run, and deleted or spammed tickets and removed articles are
forgotten automatically.

Setup (see the README for details):
  1. In Freshdesk: profile picture > Profile settings > View API key.
  2. Set:
       FRESHDESK_DOMAIN=acme          (the part before .freshdesk.com)
       FRESHDESK_API_KEY=your-key
     plus the usual cognee LLM_API_KEY.

Run:
    python examples/example.py ["optional question about your helpdesk"]
"""

import asyncio
import sys

import cognee

from cognee_community_connector_freshdesk import freshdesk_source

DATASET = "freshdesk_demo"


async def main():
    # Reads FRESHDESK_DOMAIN and FRESHDESK_API_KEY.  Private notes stay out
    # unless include_private_notes=True.
    source = freshdesk_source()

    print("=== Sync ===")
    await cognee.remember(
        source,
        dataset_name=DATASET,
        primary_key="id",
        # "merge" is required: it makes re-runs incremental and makes
        # deletions propagate through cognee's orphan cleanup.
        write_disposition="merge",
        # The default caps a table at 50 rows; a helpdesk easily exceeds it.
        max_rows_per_table=0,
    )
    # Counts only, never subjects or content.
    print("Sync stats:", source.cognee_sync_stats)

    question = sys.argv[1] if len(sys.argv) > 1 else "What problems are customers reporting?"
    answer = await cognee.recall(question, datasets=[DATASET])
    print("Recall:", answer)


if __name__ == "__main__":
    asyncio.run(main())
