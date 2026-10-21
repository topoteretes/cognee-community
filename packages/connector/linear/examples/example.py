"""Example demonstrating how to sync Linear issues into Cognee memory and query the knowledge graph.

Prerequisites:
  export LINEAR_API_KEY="lin_api_xxxxxxxxxxxxxxxx"
  pip install cognee-community-connector-linear "dlt[sqlalchemy]"
"""

import asyncio
import os

import cognee

from cognee_community_connector_linear import linear_source

DATASET_NAME = "linear_engineering_memory"


async def main():
    api_key = os.environ.get("LINEAR_API_KEY")
    if not api_key:
        print("Please set LINEAR_API_KEY environment variable to run this example.")
        return

    # Ingest issues from Engineering team
    source = linear_source(api_key=api_key)

    print("Syncing Linear issues into Cognee memory ...")
    await cognee.remember(source, dataset_name=DATASET_NAME, self_improvement=False)

    print("\nQuerying knowledge graph ...")
    answer = await cognee.search(
        query_text="What are the highest priority bugs currently assigned to engineers?",
        query_type=cognee.SearchType.GRAPH_COMPLETION,
        datasets=[DATASET_NAME],
    )
    print("\nSearch result:\n", answer)


if __name__ == "__main__":
    asyncio.run(main())
