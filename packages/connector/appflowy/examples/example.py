"""Example demonstrating AppFlowy knowledge base, workspace & database ingestion into Cognee.

This script fetches workspaces, hierarchical document pages, and database tables
from AppFlowy, indexes them into Cognee memory via cognify(), and queries
internal documentation and sprint records.
"""

from __future__ import annotations

import asyncio
import os

import cognee

from cognee_community_connector_appflowy import (
    AppFlowyConfig,
    appflowy_connector,
)

DATASET_NAME = "appflowy_knowledge"


async def main() -> None:
    # 1. Initialize connector configuration
    config = AppFlowyConfig(
        api_key=os.getenv("APPFLOWY_API_KEY", "af_pat_mock_example_12345"),
        base_url=os.getenv("APPFLOWY_BASE_URL", "https://api.appflowy.cloud/api/v1"),
        include_documents=True,
        include_databases=True,
    )

    print("Fetching AppFlowy workspaces, documents, and databases...")
    documents = await appflowy_connector(config)
    print(f"Retrieved {len(documents)} structured AppFlowy knowledge documents.")

    # 2. Ingest documents into Cognee dataset
    print(f"\nAdding documents to Cognee dataset '{DATASET_NAME}'...")
    await cognee.add(documents, dataset_name=DATASET_NAME)

    # 3. Cognify into knowledge graph and vector indices
    print("Cognifying dataset to build internal knowledge graph...")
    await cognee.cognify(dataset_name=DATASET_NAME)

    # 4. Search and reason over AppFlowy memory
    queries = [
        "What are the deployment steps documented in the operations wiki?",
        "Show me all high-priority tasks and bug tickets in the sprint database.",
        "What are the primary workspaces and who owns them?",
    ]

    for query in queries:
        print(f"\nQuery: '{query}'")
        results = await cognee.search(query, dataset_name=DATASET_NAME)
        for res in results:
            print(f"- {res}")


if __name__ == "__main__":
    asyncio.run(main())
