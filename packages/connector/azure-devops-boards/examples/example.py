"""Azure DevOps Boards into cognee, then ask the backlog a question.

Setup:

    export AZURE_DEVOPS_PAT="..."        # token with Work Items (Read)
    export AZURE_DEVOPS_ORG="my-org"
    export AZURE_DEVOPS_PROJECT="my-project"
    export LLM_API_KEY="..."
    uv run python examples/example.py

Run it again after editing, commenting on or deleting a work item. Only what
changed is fetched, and deleted work items are forgotten.
"""

import asyncio
import os

import cognee

from cognee_community_connector_azure_devops_boards import azure_devops_boards_source

DATASET_NAME = "azure_devops_boards"


async def main() -> None:
    missing = [
        name
        for name in ("AZURE_DEVOPS_PAT", "AZURE_DEVOPS_ORG", "AZURE_DEVOPS_PROJECT")
        if not os.environ.get(name)
    ]
    if missing:
        print("Set " + ", ".join(missing) + " to run this example.")
        return

    source = azure_devops_boards_source(
        organization=os.environ["AZURE_DEVOPS_ORG"],
        project=os.environ["AZURE_DEVOPS_PROJECT"],
    )

    print("Syncing work items into cognee...")
    # merge is required: the connector only sends what changed since last run.
    await cognee.remember(source, dataset_name=DATASET_NAME, write_disposition="merge")

    question = "Which work items are still open, and who is working on them?"
    answer = await cognee.search(
        query_text=question,
        query_type=cognee.SearchType.GRAPH_COMPLETION,
        datasets=[DATASET_NAME],
    )
    print(f"\n{question}\n{answer}")


if __name__ == "__main__":
    asyncio.run(main())
