"""Wrike connector demo — turn Wrike folders and tasks into memory.

``wrike_source`` returns a ``dlt`` source you hand straight to ``cognee.remember``.
Every task and folder is ingested as a document (status, assignees, custom fields,
description and comments, all by name), so it goes through normal cognify.

The first run ingests everything in scope. Re-running only syncs what changed since
the last run, and tasks you delete in Wrike, move out of the selected folders or
remove with their folder are forgotten.

────────────────────────────────────────────────────────────────────────────
Privacy / opt-in
────────────────────────────────────────────────────────────────────────────
This reads your Wrike tasks, descriptions and comments. Nothing is fetched until you
run this script. Scope what you ingest with ``WRIKE_FOLDER_IDS``, and use a dedicated
dataset so you can wipe it with a single ``cognee.forget``.

────────────────────────────────────────────────────────────────────────────
One-time setup
────────────────────────────────────────────────────────────────────────────
1. In Wrike, open Apps & Integrations → API, create an app and a permanent access
   token (see the README for OAuth instead).
2. Export the token, optionally the folders to sync, and your LLM key, then run:

       export WRIKE_ACCESS_TOKEN="..."
       export WRIKE_FOLDER_IDS="MQAAAAERJAK1"   # optional: omit to sync everything
       export LLM_API_KEY="..."                 # optional: without it cognee runs keyless
       uv run python examples/example.py

Edit a task, add a comment or delete a task in Wrike, then re-run to see the re-sync
and forget-on-delete.
"""

import asyncio
import os

import cognee

from cognee_community_connector_wrike import wrike_source

# Keep Wrike in its own dataset so it is easy to inspect and forget.
DATASET_NAME = "wrike"


async def main() -> None:
    if not os.environ.get("WRIKE_ACCESS_TOKEN"):
        print("Set WRIKE_ACCESS_TOKEN (a Wrike permanent access token) to run this example.")
        return

    print("Syncing Wrike into cognee ...")
    await cognee.remember(
        wrike_source(),  # reads WRIKE_* env vars
        dataset_name=DATASET_NAME,
        primary_key="id",
        # "merge" makes re-runs incremental and lets deletions propagate; the
        # default ("replace") would re-extract every task on every run.
        write_disposition="merge",
        # The DLT ingestion default caps a table at 50 rows; lift the cap.
        max_rows_per_table=0,
        # Skip the improve() enrichment pass, which costs LLM calls on every sync.
        self_improvement=False,
    )

    results = await cognee.recall(
        "What are the open tasks and what was discussed in their comments?",
        datasets=[DATASET_NAME],
    )
    print("\nRecall:")
    for result in results[:3]:
        print("-", result.text.strip().replace("\n", " | ")[:200])

    print(
        "\nEdit, comment on or delete a task in Wrike, then re-run: only the changes are "
        "synced, and deleted tasks are forgotten."
    )


if __name__ == "__main__":
    asyncio.run(main())
