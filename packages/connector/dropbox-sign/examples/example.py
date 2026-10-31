"""Dropbox Sign connector demo: turn signature requests into memory.

``dropbox_sign_source`` returns a ``dlt`` source you hand straight to
``cognee.remember``. Every signature request (title, status, dates, message, and
each signer's own status) is ingested as a normal document. Form field values,
custom fields and file URLs are never read.

Re-running syncs only what changed: a request is sent again when a signer views,
signs or declines it, and canceled requests are forgotten.

Privacy / opt-in
----------------
Signature requests name people and the agreements they sign. Nothing is fetched
until you run this script. Keep Dropbox Sign in its own dataset.

One-time setup
--------------
Create an API key at https://app.hellosign.com (Settings, API), then export it with
your LLM key:

    export DROPBOX_SIGN_API_KEY="..."
    export LLM_API_KEY="sk-..."
    uv run python examples/example.py
"""

import asyncio
import os

import cognee

from cognee_community_connector_dropbox_sign import dropbox_sign_source

DATASET_NAME = "dropbox_sign"


async def main() -> None:
    if not os.environ.get("DROPBOX_SIGN_API_KEY"):
        print("Set DROPBOX_SIGN_API_KEY to run this example.")
        return

    # Narrow the sync with query="title:NDA", account_id="all" or since_days=...
    source = dropbox_sign_source(api_key=os.environ["DROPBOX_SIGN_API_KEY"])

    print("Syncing Dropbox Sign into cognee ...")
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
        "Which agreements are still waiting for a signature, and from whom?",
        datasets=[DATASET_NAME],
    )
    print("\nRecall result:\n", results)


if __name__ == "__main__":
    asyncio.run(main())
