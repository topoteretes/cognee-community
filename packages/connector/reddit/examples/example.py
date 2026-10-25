"""Runnable example of ingesting Reddit submissions into Cognee."""

import asyncio
import os
import cognee
from cognee_community_connector_reddit import reddit_source


async def main():
    # Example 1: Ingest hot posts from r/Python and r/MachineLearning
    source = reddit_source(
        subreddits=["python", "MachineLearning"],
        listing="hot",
        limit=5,
        max_comment_depth=3,
        user_agent=os.environ.get("REDDIT_USER_AGENT", "cognee-example/1.0"),
    )

    print("Adding Reddit source to Cognee memory...")
    await cognee.add(source)

    print("Cognifying ingested knowledge graph...")
    await cognee.cognify()

    print("Querying Cognee memory...")
    answers = await cognee.search("What are people discussing in Python right now?")
    for item in answers:
        print("-", item)


if __name__ == "__main__":
    asyncio.run(main())
