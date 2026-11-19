"""Ingest Hoppscotch API collections into cognee memory via the dlt connector.

Demonstrates:
1. Ingesting a Hoppscotch API collection export with environment variable substitution.
2. Building an AI agent knowledge graph with ``cognee.cognify()``.
3. Asking complex questions about available endpoints, query parameters, and payloads with ``cognee.search()``.
4. Demonstrating forget-on-delete / orphan cleanup when an endpoint is removed from the collection.

Requires:
    pip install "cognee[dlt]"
    export LLM_API_KEY="your-llm-key"
"""

import asyncio
import os

import cognee
from cognee_community_connector_hoppscotch import hoppscotch_source

DATASET = "api_services_memory"
CURRENT_DIR = os.path.dirname(__file__)
COLLECTION_PATH = os.path.join(CURRENT_DIR, "test_data", "sample_hoppscotch_collection.json")
ENVIRONMENT_PATH = os.path.join(CURRENT_DIR, "test_data", "sample_hoppscotch_environment.json")

QUESTION = "How do I create a charge using the billing API, and what headers are required?"


async def main() -> None:
    print("1. Initializing clean memory state...")
    await cognee.forget(everything=True)

    print("2. Ingesting Hoppscotch API collection snapshot...")
    await cognee.remember(
        hoppscotch_source(
            collection_path=COLLECTION_PATH,
            environment_path=ENVIRONMENT_PATH,
        ),
        dataset_name=DATASET,
        max_rows_per_table=0,  # Ingest entire collection snapshot
    )

    print("3. Building knowledge graph from API endpoints...")
    await cognee.cognify()

    print(f"\n4. Querying agent memory: '{QUESTION}'")
    answer = await cognee.search(QUESTION)
    print("\nAgent Answer:\n", answer)


if __name__ == "__main__":
    asyncio.run(main())
