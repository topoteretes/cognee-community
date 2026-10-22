"""Azure Repos pull requests into cognee, twice, so you can see the incremental sync.

Setup:

    export AZURE_DEVOPS_PAT="..."        # token with Code (Read)
    export AZURE_DEVOPS_ORG="my-org"
    export AZURE_DEVOPS_PROJECT="my-project"
    export LLM_API_KEY="..."
    uv run python examples/example.py

The first sync reads every pull request. Then comment on, vote on or merge one
and press Enter: the second sync only fetches PRs that changed.
"""

import asyncio
import os
import sys

import cognee

from cognee_community_connector_azure_repos import azure_repos_source

DATASET_NAME = "azure_repos"


async def sync() -> dict:
    source = azure_repos_source(
        organization=os.environ["AZURE_DEVOPS_ORG"],
        project=os.environ["AZURE_DEVOPS_PROJECT"],
        pat=os.environ["AZURE_DEVOPS_PAT"],
    )
    # merge is required: later runs only send PRs that changed.
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
        input("\nComment on, vote on or merge a pull request, then press Enter... ")
    print("Second sync:", await sync())

    question = "What were the last few merged changes, and what did reviewers push back on?"
    answer = await cognee.search(
        query_text=question,
        query_type=cognee.SearchType.GRAPH_COMPLETION,
        datasets=[DATASET_NAME],
    )
    print(f"\n{question}\n{answer}")


if __name__ == "__main__":
    asyncio.run(main())
