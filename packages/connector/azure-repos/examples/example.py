"""Azure Repos pull requests into cognee, then ask why a change was made.

Setup:

    export AZURE_DEVOPS_PAT="..."        # token with Code (Read)
    export AZURE_DEVOPS_ORG="my-org"
    export AZURE_DEVOPS_PROJECT="my-project"
    export LLM_API_KEY="..."
    uv run python examples/example.py

Run it again after a PR gets a comment, a vote or a merge. Only changed PRs are
fetched again.
"""

import asyncio
import os

import cognee

from cognee_community_connector_azure_repos import azure_repos_source

DATASET_NAME = "azure_repos"


async def main() -> None:
    missing = [
        name
        for name in ("AZURE_DEVOPS_PAT", "AZURE_DEVOPS_ORG", "AZURE_DEVOPS_PROJECT")
        if not os.environ.get(name)
    ]
    if missing:
        print("Set " + ", ".join(missing) + " to run this example.")
        return

    source = azure_repos_source(
        organization=os.environ["AZURE_DEVOPS_ORG"],
        project=os.environ["AZURE_DEVOPS_PROJECT"],
    )

    print("Syncing pull requests into cognee...")
    # merge is required: only changed PRs are sent on later runs.
    await cognee.remember(source, dataset_name=DATASET_NAME, write_disposition="merge")

    question = "What were the last few merged changes, and what did reviewers push back on?"
    answer = await cognee.search(
        query_text=question,
        query_type=cognee.SearchType.GRAPH_COMPLETION,
        datasets=[DATASET_NAME],
    )
    print(f"\n{question}\n{answer}")


if __name__ == "__main__":
    asyncio.run(main())
