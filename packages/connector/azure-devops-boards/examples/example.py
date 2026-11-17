"""Azure DevOps Boards into cognee, twice, so you can see the incremental sync.

Setup:

    export AZURE_DEVOPS_PAT="..."        # token with Work Items (Read)
    export AZURE_DEVOPS_ORG="my-org"
    export AZURE_DEVOPS_PROJECT="my-project"
    export LLM_API_KEY="..."
    uv run python examples/example.py

The first sync reads every work item. Then edit, comment on or delete one in
Azure DevOps and press Enter: the second sync only touches what changed.
"""

import asyncio
import os
import sys

import cognee

from cognee_community_connector_azure_devops_boards import azure_devops_boards_source

DATASET_NAME = "azure_devops_boards"


async def sync() -> dict:
    source = azure_devops_boards_source(
        organization=os.environ["AZURE_DEVOPS_ORG"],
        project=os.environ["AZURE_DEVOPS_PROJECT"],
        pat=os.environ["AZURE_DEVOPS_PAT"],
    )
    # merge is required: later runs only send what changed.
    await cognee.remember(source, dataset_name=DATASET_NAME, write_disposition="merge")
    return dict(source.cognee_sync_stats)


async def main() -> None:
    missing = [
        name
        for name in ("AZURE_DEVOPS_PAT", "AZURE_DEVOPS_ORG", "AZURE_DEVOPS_PROJECT")
        if not os.environ.get(name)
    ]
    if missing:
        print("Set " + ", ".join(missing) + " to run this example.")
        return

    print("First sync:", await sync())

    if sys.stdin.isatty():
        input("\nEdit, comment on or delete a work item in Azure DevOps, then press Enter... ")
    print("Second sync:", await sync())

    question = "Which work items are still open, and who is working on them?"
    answer = await cognee.search(
        query_text=question,
        query_type=cognee.SearchType.GRAPH_COMPLETION,
        datasets=[DATASET_NAME],
    )
    print(f"\n{question}\n{answer}")


if __name__ == "__main__":
    asyncio.run(main())
