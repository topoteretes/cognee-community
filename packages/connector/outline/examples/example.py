"""Example demonstrating Outline knowledge base ingestion into Cognee."""

import asyncio
import os

import cognee

from cognee_community_connector_outline import outline_source


async def main():
    # Configure Cognee dataset and run full snapshot ingestion from Outline
    base_url = os.environ.get("OUTLINE_URL", "https://app.getoutline.com/api")
    api_token = os.environ.get("OUTLINE_API_KEY", "ol_api_token_here")
    collection_ids = ["engineering_collection_id"]

    print("Ingesting Outline knowledge base into Cognee...")
    await cognee.remember(
        outline_source(
            base_url=base_url,
            api_token=api_token,
            collection_ids=collection_ids,
        ),
        dataset_name="outline_wiki",
    )

    print("Ingestion complete. Querying memory...")
    results = await cognee.search(
        query_text="What are our engineering deployment procedures and runbooks?",
        query_type=cognee.SearchType.GRAPH_COMPLETION,
        datasets=["outline_wiki"],
    )

    for result in results:
        print(f"Result: {result}")


if __name__ == "__main__":
    asyncio.run(main())
