"""Terraform Cloud connector demo — "ask my infra".

Pull HCP Terraform / Terraform Cloud runs into cognee memory, incrementally,
with forget-on-delete and secret-redacted plan logs.

This example is built on cognee's DLT ingestion subsystem: ``terraform_cloud_source``
returns a ``dlt`` resource that you hand straight to ``cognee.remember``. The first run
backfills recent runs for the organization's workspaces; re-running ``remember`` syncs
only runs created since (via ``created-at``), and runs whose workspace you delete in
Terraform Cloud are forgotten from memory on the next sync.

────────────────────────────────────────────────────────────────────────────
One-time setup
────────────────────────────────────────────────────────────────────────────
1. Install the extra:

       pip install "cognee[terraform-cloud]"   # or: uv sync --extra terraform-cloud

2. Create a Terraform Cloud API token at
   https://app.terraform.io/app/settings/tokens

3. Export your connection details (the token is read-only here — the connector
   only issues GET requests):

       export TFC_TOKEN="…"
       export TFC_ORGANIZATION="my-org"
       # optional: export TFC_WORKSPACES="prod,staging"

4. Set your LLM key (``LLM_API_KEY``) in ``.env`` like any other cognee example.

Run it:

    uv run python examples/example.py
"""

import asyncio
import os

import cognee

from cognee_community_connector_terraform_cloud import terraform_cloud_source

# Keep Terraform runs in their own dataset so they are easy to inspect and forget.
DATASET_NAME = "terraform"

# Routing kwargs shared by every remember() call below. ``max_rows_per_table=0``
# disables cognee's per-table read cap so orphan-cleanup (forget-on-delete)
# compares against the *entire* synced corpus, not a 50-row window.
TFC_REMEMBER_KWARGS = {
    "primary_key": "id",
    "write_disposition": "merge",
    "max_rows_per_table": 0,
}


async def main() -> None:
    token = os.environ.get("TFC_TOKEN")
    organization = os.environ.get("TFC_ORGANIZATION")
    if not (token and organization):
        print("Set TFC_TOKEN and TFC_ORGANIZATION to run this example.")
        return

    workspaces = os.environ.get("TFC_WORKSPACES")
    workspace_names = (
        [w.strip() for w in workspaces.split(",") if w.strip()] if workspaces else None
    )

    source = terraform_cloud_source(
        organization=organization,
        token=token,
        workspace_names=workspace_names,
    )

    print("Syncing Terraform Cloud runs into cognee ...")
    await cognee.remember(source, dataset_name=DATASET_NAME, **TFC_REMEMBER_KWARGS)

    answer = await cognee.search(
        query_text="Summarize the most recent Terraform runs and flag any failures.",
        query_type=cognee.SearchType.GRAPH_COMPLETION,
        datasets=[DATASET_NAME],
    )
    print("\nSearch result:\n", answer)

    print(
        "\nTrigger a new run (or delete a workspace) in Terraform Cloud, then re-run: "
        "new runs sync incrementally and runs from a removed workspace are reconciled "
        "out of memory."
    )


if __name__ == "__main__":
    asyncio.run(main())
