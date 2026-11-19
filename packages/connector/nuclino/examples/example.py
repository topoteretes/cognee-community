"""Nuclino connector demo — turn your Nuclino workspace into Cognee memory.

Pulls Nuclino items and collections into Cognee as document-mode sources,
with incremental synchronization and forget-on-delete.

Requirements:
    1. Install package:
       uv pip install cognee-community-connector-nuclino
       # or for local development:
       cd packages/connector/nuclino && uv sync

    2. Set environment variables:
       export NUCLINO_API_KEY="your-nuclino-api-key"
       export LLM_API_KEY="your-llm-api-key"  # or provider-specific LLM key

Re-running this script only fetches changed or new items, and items deleted
from Nuclino are automatically forgotten from Cognee memory.
"""

import asyncio
import os

import cognee

from cognee_community_connector_nuclino import nuclino_source

DATASET_NAME = "nuclino_demo"


async def main() -> None:
    if not os.environ.get("NUCLINO_API_KEY"):
        print("Please set the NUCLINO_API_KEY environment variable to run this example.")
        return

    # By default, nuclino_source() discovers and syncs all accessible workspaces.
    # To restrict sync to specific workspaces, pass workspace_ids:
    #   source = nuclino_source(workspace_ids=["<workspace-id-1>", "<workspace-id-2>"])
    # Or filter by team:
    #   source = nuclino_source(team_id="<team-id>")
    source = nuclino_source()

    print("Syncing Nuclino items and collections into Cognee...")
    await cognee.remember(
        source,
        dataset_name=DATASET_NAME,
        primary_key="id",
        # "merge" is required: it enables incremental upserting by primary key
        # and allows _deleted tombstones to trigger Cognee's orphan cleanup.
        write_disposition="merge",
    )
    print("Sync complete.")

    print("\nQuerying Cognee memory:")
    result = await cognee.recall("What information is stored in my Nuclino workspace?")
    print(result)

    print(
        "\nEdit or delete an item in Nuclino, then re-run this script: "
        "modifications are updated and deleted items are removed from memory."
    )


if __name__ == "__main__":
    asyncio.run(main())
