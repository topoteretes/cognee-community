"""Coda connector demo: turn Coda docs into memory.

``coda_source`` returns a ``dlt`` source you hand straight to ``cognee.remember``.
Every page (exported as markdown) and every table (rows as labeled blocks) is
ingested as a normal document, so it goes through cognify entity extraction.

Re-running syncs only what changed: edited pages and tables are processed again
and deleted ones are forgotten.

Privacy / opt-in
----------------
This reads the content of the selected docs. Nothing is fetched until you run
this script. Use a read-only token and keep Coda in its own dataset.

One-time setup
--------------
Create an API token under My account in Coda, then export it, the doc to sync
(the part after ``_d`` in the doc URL) and your LLM key, and run:

    export CODA_API_TOKEN="..."
    export CODA_DOC_ID="..."
    export LLM_API_KEY="sk-..."
    uv run python examples/example.py
"""

import asyncio
import os

import cognee

from cognee_community_connector_coda import coda_source

DATASET_NAME = "coda"


async def main() -> None:
    required = ("CODA_API_TOKEN", "CODA_DOC_ID")
    if not all(os.environ.get(name) for name in required):
        print(f"Set {', '.join(required)} to run this example.")
        return

    source = coda_source(
        doc_ids=[os.environ["CODA_DOC_ID"]],
        token=os.environ["CODA_API_TOKEN"],
        base_url=os.environ.get("CODA_API_BASE_URL"),
    )

    print("Syncing Coda into cognee ...")
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
        "What are the open tasks in this doc and who owns them?", datasets=[DATASET_NAME]
    )
    print("\nRecall result:\n", results)

    print(
        "\nEdit a page or a table row, or delete a page, then re-run: only the changes are "
        "synced and deleted pages and tables are forgotten."
    )


if __name__ == "__main__":
    asyncio.run(main())
