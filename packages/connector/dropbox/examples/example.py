"""Sync a Dropbox folder into cognee memory, incrementally.

Text, markdown, CSV, PDF files and Paper docs are extracted, chunked and
cognified like any other document.  Re-running this script only processes
files that changed since the last run, and files deleted from Dropbox (or
moved out of the synced folders) are forgotten automatically.

Setup:
  1. Create an app at https://www.dropbox.com/developers/apps
     (Scoped access, App folder or Full Dropbox) and enable the
     files.metadata.read and files.content.read permissions.
  2. Get a refresh token once:
       DROPBOX_APP_KEY=<app key> python examples/get_refresh_token.py
  3. Set:
       DROPBOX_APP_KEY=<app key>
       DROPBOX_REFRESH_TOKEN=<refresh token>
       DROPBOX_FOLDER_PATHS=/Notes          (optional, comma-separated;
                                             default is the whole Dropbox)
     plus the usual cognee LLM_API_KEY (see cognee's .env.template).

Run:
    python examples/example.py ["optional question about your files"]
"""

import asyncio
import sys

import cognee

from cognee_community_connector_dropbox import dropbox_source

DATASET = "dropbox_demo"


async def main():
    source = dropbox_source()  # reads DROPBOX_* env vars

    print("=== Sync ===")
    await cognee.remember(
        source,
        dataset_name=DATASET,
        primary_key="id",
        # "merge" is required: it makes re-runs incremental and makes
        # deletions propagate through cognee's orphan cleanup.
        write_disposition="merge",
        # The default caps a table at 50 rows; Dropbox folders often exceed it.
        max_rows_per_table=0,
    )
    # Counts only, never file names or content.
    print("Sync stats:", source.cognee_sync_stats)

    question = sys.argv[1] if len(sys.argv) > 1 else "Summarize what is in my Dropbox files."
    answer = await cognee.recall(question, datasets=[DATASET])
    print("Recall:", answer)


if __name__ == "__main__":
    asyncio.run(main())
