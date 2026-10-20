"""Example demonstrating Appwrite database collection ingestion into Cognee."""

import asyncio
import os

import cognee

from cognee_community_connector_appwrite import appwrite_source


async def main():
    # Configure Cognee dataset and run full snapshot ingestion
    endpoint = os.environ.get("APPWRITE_ENDPOINT", "https://cloud.appwrite.io/v1")
    project_id = os.environ.get("APPWRITE_PROJECT_ID", "my-project-id")
    api_key = os.environ.get("APPWRITE_API_KEY", "my-appwrite-api-key")
    database_id = os.environ.get("APPWRITE_DATABASE_ID", "default_db")
    collection_ids = ["articles", "documentation"]

    print("Ingesting Appwrite collections into Cognee...")
    await cognee.remember(
        appwrite_source(
            endpoint=endpoint,
            project_id=project_id,
            api_key=api_key,
            database_id=database_id,
            collection_ids=collection_ids,
        ),
        dataset_name="appwrite_docs",
    )

    print("Ingestion complete. Querying memory...")
    results = await cognee.search(
        query_text="What are our core architectural guidelines and components?",
        query_type=cognee.SearchType.GRAPH_COMPLETION,
        datasets=["appwrite_docs"],
    )

    for result in results:
        print(f"Result: {result}")


if __name__ == "__main__":
    asyncio.run(main())
