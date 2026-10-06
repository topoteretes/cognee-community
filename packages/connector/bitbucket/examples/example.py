"""Sync selected Bitbucket Cloud content into cognee memory.

Set BITBUCKET_ACCESS_TOKEN for OAuth bearer auth, or BITBUCKET_EMAIL and
BITBUCKET_API_TOKEN for Atlassian API-token auth. Also set the usual
LLM_API_KEY for cognee.
"""

import asyncio
import os

import cognee

from cognee_community_connector_bitbucket import bitbucket_source

WORKSPACE = os.environ.get("BITBUCKET_WORKSPACE", "my-workspace")
REPOSITORIES = [
    repo.strip()
    for repo in os.environ.get("BITBUCKET_REPOSITORIES", "service-api").split(",")
    if repo.strip()
]
DATASET = "bitbucket"


async def sync() -> None:
    has_oauth = bool(os.environ.get("BITBUCKET_ACCESS_TOKEN"))
    has_api_token = bool(
        os.environ.get("BITBUCKET_API_TOKEN") and os.environ.get("BITBUCKET_EMAIL")
    )
    if not (has_oauth or has_api_token):
        print(
            "Set BITBUCKET_ACCESS_TOKEN, or both BITBUCKET_API_TOKEN and "
            "BITBUCKET_EMAIL. Also set LLM_API_KEY."
        )
        return

    await cognee.remember(
        bitbucket_source(
            workspace=WORKSPACE,
            repositories=REPOSITORIES,
            content_types=["pull_requests", "comments", "wiki"],
        ),
        dataset_name=DATASET,
        primary_key="id",
        write_disposition="merge",
        max_rows_per_table=0,
    )

    answer = await cognee.search(
        query_text="What changed in the recent pull requests?",
        query_type=cognee.SearchType.GRAPH_COMPLETION,
        datasets=[DATASET],
    )
    print("\nSearch result:\n", answer)


if __name__ == "__main__":
    asyncio.run(sync())
