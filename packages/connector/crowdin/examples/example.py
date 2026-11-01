"""Example demonstrating Crowdin localization & glossary ingestion into Cognee.

This script fetches projects, terminology glossaries, and localized strings from
a Crowdin account, indexes them into Cognee memory via cognify(), and executes
multilingual terminology queries.
"""

from __future__ import annotations

import asyncio
import os

import cognee

from cognee_community_connector_crowdin import (
    CrowdinConfig,
    crowdin_connector,
)

DATASET_NAME = "localization_knowledge"


async def main() -> None:
    # 1. Initialize connector configuration
    config = CrowdinConfig(
        api_token=os.getenv("CROWDIN_PERSONAL_TOKEN", "crowdin_pat_example"),
        include_glossaries=True,
        include_strings=True,
    )

    print("Fetching Crowdin projects, glossaries, and localization strings...")
    documents = await crowdin_connector(config)
    print(f"Retrieved {len(documents)} structured localization documents.")

    # 2. Ingest documents into Cognee dataset
    print(f"\nAdding documents to Cognee dataset '{DATASET_NAME}'...")
    await cognee.add(documents, dataset_name=DATASET_NAME)

    # 3. Cognify into knowledge graph and vector indices
    print("Cognifying dataset to build localization & terminology graph...")
    await cognee.cognify(dataset_name=DATASET_NAME)

    # 4. Search and reason over multilingual memory
    queries = [
        "What is the approved terminology for Passkey in German?",
        "Show me all translated login and authentication strings.",
        "What target languages are configured for the primary project?",
    ]

    for query in queries:
        print(f"\nQuery: '{query}'")
        results = await cognee.search(query, dataset_name=DATASET_NAME)
        for res in results:
            print(f"- {res}")


if __name__ == "__main__":
    asyncio.run(main())
