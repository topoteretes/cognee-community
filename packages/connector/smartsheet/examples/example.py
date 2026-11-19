"""Sync Smartsheet sheets into cognee memory.

Each selected sheet becomes a summary document plus one document per row
that carries real content (free text, a comment, or an attachment) -- a
sheet full of empty/numeric rows does not become a wall of near-empty
documents.

Re-running this script is incremental: a sheet is only fully re-fetched if
its version changed, on a periodic full pass, or via a small rotating
re-check (see "How sync + forget-on-delete work" in the README). Try
deleting a row (or the whole sheet) in Smartsheet and running this again --
the corresponding document is removed from the graph, not just left stale.

Setup (see the README for details):
  1. Generate a personal access token: Account -> Personal Settings -> API
     Access. Requires a paid Business, Enterprise, or Advanced Work
     Management plan -- the free trial and Pro plan cannot generate tokens.
  2. Set:
       SMARTSHEET_API_TOKEN=...
       SMARTSHEET_SHEET_IDS=1234567890123456          (comma-separated)
     plus the usual cognee LLM_API_KEY. Set SMARTSHEET_HOST only if your
     account is not on the US region (api.smartsheet.com).

Run:
    python examples/example.py ["optional question about your sheets"]
"""

import asyncio
import os
import sys

import cognee

from cognee_community_connector_smartsheet import smartsheet_source

DATASET = "smartsheet_demo"


def _parse_ids(value: str | None) -> list[int] | None:
    if not value:
        return None
    return [int(piece.strip()) for piece in value.split(",") if piece.strip()]


async def main() -> None:
    if not os.environ.get("SMARTSHEET_API_TOKEN"):
        print("Set SMARTSHEET_API_TOKEN to run this example.")
        return

    sheet_ids = _parse_ids(os.environ.get("SMARTSHEET_SHEET_IDS"))
    folder_ids = _parse_ids(os.environ.get("SMARTSHEET_FOLDER_IDS"))
    workspace_ids = _parse_ids(os.environ.get("SMARTSHEET_WORKSPACE_IDS"))
    if not (sheet_ids or folder_ids or workspace_ids):
        print(
            "Set one of SMARTSHEET_SHEET_IDS, SMARTSHEET_FOLDER_IDS, or "
            "SMARTSHEET_WORKSPACE_IDS (comma-separated ids) to run this example."
        )
        return

    source = smartsheet_source(
        sheet_ids=sheet_ids, folder_ids=folder_ids, workspace_ids=workspace_ids
    )

    print("=== Sync ===")
    await cognee.remember(
        source,
        dataset_name=DATASET,
        primary_key="id",
        # "merge" is required: cognee.remember defaults to "replace" otherwise.
        write_disposition="merge",
        # The default caps a table at 50 rows; a sheet easily exceeds it.
        max_rows_per_table=0,
    )
    # Counts only, never sheet/row content.
    print("Sync stats:", source.cognee_sync_stats)

    question = sys.argv[1] if len(sys.argv) > 1 else "What sheets do I have, and what's on them?"
    answer = await cognee.recall(question, datasets=[DATASET])
    print("Recall:", answer)


if __name__ == "__main__":
    asyncio.run(main())
