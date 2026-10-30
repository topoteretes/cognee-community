"""Sync GitBook docs into cognee, then search them.

Set GITBOOK_API_TOKEN and LLM_API_KEY, optionally GITBOOK_ORG_ID,
then run: python examples/example.py. Re-run after edits or deletions.
"""

import asyncio
import os

import cognee

from cognee_community_connector_gitbook import gitbook_source

DATASET_NAME = "gitbook"


async def main() -> None:
    if not os.environ.get("GITBOOK_API_TOKEN"):
        print("Set GITBOOK_API_TOKEN to a GitBook personal access token.")
        return
    print("Syncing GitBook sites and pages into cognee ...")
    await cognee.remember(gitbook_source(), dataset_name=DATASET_NAME)
    answer = await cognee.search(
        query_text="How do I get started with our product?",
        query_type=cognee.SearchType.GRAPH_COMPLETION,
        datasets=[DATASET_NAME],
    )
    print("\nSearch result:\n", answer)
    print("\nEdit or delete content in GitBook, then re-run to sync the changes.")


if __name__ == "__main__":
    asyncio.run(main())
