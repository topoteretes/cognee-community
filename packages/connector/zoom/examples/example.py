"""Zoom connector demo: turn your Zoom cloud recordings into memory.

``zoom_source`` returns a ``dlt`` source you hand straight to ``cognee.remember``.
Each recorded meeting (topic, date, host, transcript and in-meeting chat) is
ingested as a normal document, so it goes through cognify entity extraction.

Re-running syncs only what changed: new recordings are added, a transcript that
arrives late updates its meeting, and recordings you delete in Zoom are
forgotten.

Privacy / opt-in
----------------
This reads meeting transcripts and chat, which are personal data. Nothing is
fetched until you run this script. Keep Zoom in its own dataset and limit the
scope with ``user_ids`` and ``since`` if you only need part of the account.

One-time setup
--------------
1. Create a Server-to-Server OAuth app in the Zoom App Marketplace with the
   scopes listed in the README, and activate it.
2. Export its credentials and your LLM key, then run:

       export ZOOM_ACCOUNT_ID="..."
       export ZOOM_CLIENT_ID="..."
       export ZOOM_CLIENT_SECRET="..."
       export LLM_API_KEY="sk-..."
       uv run python examples/example.py
"""

import asyncio
import os

import cognee

from cognee_community_connector_zoom import zoom_source

DATASET_NAME = "zoom"


async def main() -> None:
    required = ("ZOOM_ACCOUNT_ID", "ZOOM_CLIENT_ID", "ZOOM_CLIENT_SECRET")
    if not all(os.environ.get(name) for name in required):
        print(f"Set {', '.join(required)} to run this example.")
        return

    # Narrow the sync with user_ids=[...] or since="YYYY-MM-DD" on a big account.
    source = zoom_source()

    print("Syncing Zoom cloud recordings into cognee ...")
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
        "What were the main decisions in these meetings?", datasets=[DATASET_NAME]
    )
    print("\nRecall result:\n", results)

    print(
        "\nRecord a new meeting or delete a recording in Zoom, then re-run: only the "
        "changes are synced and deleted recordings are forgotten."
    )


if __name__ == "__main__":
    asyncio.run(main())
