"""Smartsheet connector demo — turn your sheets into cognee memory.

Sync Smartsheet sheets — rows rendered as readable documents, with row
discussions and attachment descriptions folded in. ``smartsheet_source``
returns a ``dlt`` source you hand straight to ``cognee.remember``.

Each run is incremental: sheets whose ``modifiedAt`` did not advance are
skipped, changed sheets are swept, and rows deleted upstream are reconciled
out of memory (forget-on-delete).

────────────────────────────────────────────────────────────────────────────
Privacy / opt-in
────────────────────────────────────────────────────────────────────────────
This reads only the sheets your token can see. It is strictly opt-in —
nothing is fetched until you run this script — and documents go into a
dedicated dataset so you can wipe it with a single ``cognee.forget(dataset)``.

────────────────────────────────────────────────────────────────────────────
One-time setup
────────────────────────────────────────────────────────────────────────────
1. Install the connector:

       cd packages/connector/smartsheet && uv sync

2. Generate an API access token (Account → Apps & Integrations → API Access).
3. Export your credentials and LLM key, then run:

       export SMARTSHEET_TOKEN="..."
       export LLM_API_KEY="sk-..."
       uv run python examples/example.py

Re-run after editing sheets to see the incremental sync; delete a row
upstream and re-run to see forget-on-delete.
"""

import asyncio
import os

import cognee

from cognee_community_connector_smartsheet import smartsheet_source

# Keep sheets in their own dataset so they are easy to inspect and forget.
DATASET_NAME = "sheets"

# None = every sheet the token can see. Prefer an explicit list on large
# accounts: sheet ids are the numbers in each sheet's URL.
SHEET_IDS = None


async def main() -> None:
    if not os.environ.get("SMARTSHEET_TOKEN"):
        print("Set SMARTSHEET_TOKEN (and LLM_API_KEY) to run this example.")
        return

    print("Syncing Smartsheet sheets into cognee ...")
    await cognee.remember(
        smartsheet_source(sheet_ids=SHEET_IDS),
        dataset_name=DATASET_NAME,
        primary_key="id",
        write_disposition="merge",
        max_rows_per_table=0,  # unlimited read-back so deletions reconcile fully
    )

    answer = await cognee.search(
        query_text="Summarize the open tasks across my sheets and their owners.",
        query_type=cognee.SearchType.GRAPH_COMPLETION,
        datasets=[DATASET_NAME],
    )
    print("\nSearch result:\n", answer)

    print(
        "\nRe-run after editing a sheet: only rows with a newer modifiedAt are "
        "re-ingested, and rows deleted upstream are reconciled out of memory."
    )


if __name__ == "__main__":
    asyncio.run(main())
