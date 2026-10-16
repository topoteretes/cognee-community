"""Terraform Cloud connector demo: turn HCP Terraform workspaces and runs into memory.

``terraform_cloud_source`` returns a ``dlt`` source you hand straight to
``cognee.remember``. Every workspace and every finished run (status, commit,
plan and apply counts, planned resource changes, diagnostics) is ingested as a
normal document, with secrets scrubbed first.

Re-running syncs only what changed: new finished runs are added and deleted
workspaces are forgotten together with their runs.

Privacy / opt-in
----------------
Plans and logs describe your infrastructure. Nothing is fetched until you run
this script. Use a read-only team token and keep Terraform in its own dataset.

One-time setup
--------------
Export a team or user token, the organization name and your LLM key, then run:

    export TFE_TOKEN="..."
    export TFE_ORGANIZATION="acme"
    export LLM_API_KEY="sk-..."
    uv run python examples/example.py
"""

import asyncio
import os

import cognee

from cognee_community_connector_terraform_cloud import terraform_cloud_source

DATASET_NAME = "terraform"


async def main() -> None:
    required = ("TFE_TOKEN", "TFE_ORGANIZATION")
    if not all(os.environ.get(name) for name in required):
        print(f"Set {', '.join(required)} to run this example.")
        return

    # Narrow the sync with workspace_names=[...], workspace_tags=[...] or since_days=...
    source = terraform_cloud_source()

    print("Syncing HCP Terraform into cognee ...")
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
        "Which workspaces changed recently, and did any run fail?", datasets=[DATASET_NAME]
    )
    print("\nRecall result:\n", results)

    print(
        "\nQueue a run or delete a workspace, then re-run: only finished new runs are "
        "added and deleted workspaces are forgotten with their runs."
    )


if __name__ == "__main__":
    asyncio.run(main())
